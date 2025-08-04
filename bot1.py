import logging
import json
import subprocess
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from pathlib import Path
import re
import time
import uuid
import math
from pymongo import MongoClient
from pymongo.errors import ConnectionError

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния разговора
(
    CHOOSE_TYPE, INPUT_FLIGHT_TIME, INPUT_SPEED, INPUT_PAYLOAD,
    INPUT_AERO_QUALITY, INPUT_THRUST_RESERVE, INPUT_MANEUVER_TIME,
    INPUT_PLANE_MATERIAL, INPUT_PROPELLER_TYPE, INPUT_TAKEOFF_TYPE,
    INPUT_CEILING, CALCULATE, CHANGE_FLIGHT_TIME, CHANGE_SPEED,
    CHANGE_AERO_QUALITY, CHANGE_MANEUVER_TIME, WELCOME_STATE,
    SHOW_HISTORY, SHOW_CONFIG, CONFIRM_DELETE, INPUT_CONFIG_NAME
) = range(21)

# Токен бота из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
load_dotenv()
GIT_TOKEN = os.getenv("GIT_TOKEN")
CONFIG_FILE = 'configurations.json'
MONGO_URI = os.getenv("MONGO_URI")  # Строка подключения к MongoDB Atlas
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["bot_configs"]
collection = db["users"]


# Стандартная атмосфера (на уровне моря)
STD_ATMOSPHERE = {
    'density': 1.225  # кг/м³ на уровне моря
}

# Словарь для маппинга выбора
SELECTION_MAPS = {
    'aero_quality': {"6": 6, "8": 8, "12": 12, "14": 14},
    'thrust_reserve': {"1.5": 1.5, "2.0": 2.0, "3.0": 3.0},
    'plane_material': {"0.40": 0.40, "0.45": 0.45, "0.50": 0.50},
    'propeller_eff': {"0.75": 0.75, "0.80": 0.80},
    'takeoff_type': {"0.3": 0.3, "0.4": 0.4, "0.6": 0.6}
}

def calculate_air_density(altitude):
    """Расчет плотности воздуха по модели ISA"""
    rho_0 = 1.225  # кг/м³ на уровне моря
    T_0 = 288.15   # К на уровне моря
    g = 9.81       # м/с²
    R = 287.05     # Дж/(кг·К)
    L = 0.0065     # К/м (температурный градиент)
    exponent = g / (R * L)
    return rho_0 * (1 - L * altitude / T_0) ** exponent if altitude <= 11000 else 0.3639 * math.exp(-g * (altitude - 11000) / (R * 226.32))

def load_configs():
    """Загрузка конфигураций из MongoDB"""
    try:
        configs = {}
        for doc in collection.find():
            user_id = str(doc["user_id"])
            configs[user_id] = doc.get("configs", {})
        return configs
    except ConnectionError as e:
        logger.error(f"Ошибка подключения к MongoDB: {e}")
        return {}
    except Exception as e:
        logger.error(f"Ошибка чтения конфигураций из MongoDB: {e}")
        return {}

def save_configs(configs):
    """Сохранение конфигураций в MongoDB"""
    try:
        for user_id, user_configs in configs.items():
            collection.update_one(
                {"user_id": user_id},
                {"$set": {"configs": user_configs}},
                upsert=True
            )
        logger.info("Конфигурации успешно сохранены в MongoDB")
    except ConnectionError as e:
        logger.error(f"Ошибка подключения к MongoDB при сохранении: {e}")
    except Exception as e:
        logger.error(f"Ошибка записи конфигураций в MongoDB: {e}")


async def delete_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int, keep_ids: list = None):
    """Удаление всех сообщений, кроме указанных в keep_ids"""
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    keep_ids = keep_ids or []
    
    deleted_count = 0
    failed_count = 0
    
    message_ids_to_delete = list(set(context.user_data['message_ids']))
    logger.info(f"Попытка удаления сообщений: {message_ids_to_delete}, сохраняемые ID: {keep_ids}")
    
    for msg_id in message_ids_to_delete:
        if msg_id not in keep_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                deleted_count += 1
                if msg_id in context.user_data['message_ids']:
                    context.user_data['message_ids'].remove(msg_id)
                logger.debug(f"Успешно удалено сообщение {msg_id}")
            except Exception as e:
                failed_count += 1
                logger.warning(f"Не удалось удалить сообщение {msg_id}: {str(e)}")
                if "message to delete not found" in str(e).lower() or "message is too old" in str(e).lower():
                    if msg_id in context.user_data['message_ids']:
                        context.user_data['message_ids'].remove(msg_id)
                    logger.debug(f"Сообщение {msg_id} удалено из message_ids, так как оно не найдено или слишком старое")
    
    logger.info(f"Удалено {deleted_count} сообщений, не удалось удалить {failed_count} сообщений")

async def send_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                      reply_markup=None, parse_mode=None):
    """Универсальная функция отправки сообщений с регистрацией message_id"""
    chat_id = update.effective_chat.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id пользователя {update.message.message_id} в message_ids")
    
    try:
        sent_msg = None
        if update.callback_query:
            await update.callback_query.answer()
            message_id = update.callback_query.message.message_id
            try:
                sent_msg = await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
                logger.debug(f"Отредактировано сообщение {sent_msg.message_id}")
            except Exception as e:
                logger.warning(f"Не удалось отредактировать сообщение {message_id}: {e}")
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
                logger.debug(f"Отправлено новое сообщение {sent_msg.message_id} вместо редактирования")
        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
            logger.debug(f"Отправлено сообщение {sent_msg.message_id}")
        
        if sent_msg.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(sent_msg.message_id)
            logger.debug(f"Добавлен message_id {sent_msg.message_id} в message_ids")
        logger.debug(f"Текущее состояние message_ids после отправки: {context.user_data['message_ids']}")
        return sent_msg
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {e}")
        raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка команды /start"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if 'last_start_time' in context.user_data:
        last_time = context.user_data['last_start_time']
        if (datetime.now() - last_time).total_seconds() < 2:
            logger.info(f"Пользователь {user_id} отправил /start слишком быстро, игнорируем")
            return WELCOME_STATE
    
    context.user_data['last_start_time'] = datetime.now()
    context.user_data['message_ids'] = []
    
    welcome_text = """
🚀 *DroneDesigner* — Telegram-бот для расчёта параметров БПЛА

• Масса конструкции
• Требуемая мощность
• Параметры батареи
• Размах и площадь крыла
• Практический потолок и плотность воздуха

Для инженеров и энтузиастов БПЛА!
    """
    
    welcome_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=welcome_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")]
        ]),
        parse_mode="Markdown"
    )
    
    context.user_data['welcome_message_id'] = welcome_msg.message_id
    context.user_data['message_ids'] = [welcome_msg.message_id]
    logger.info(f"Пользователь {user_id} запустил бот, отправлено приветственное сообщение {welcome_msg.message_id}")
    return WELCOME_STATE

async def handle_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка действий на приветственном экране"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для обработки приветственного экрана")

    if query.data not in ["history", "new_config", "back_to_welcome"]:
        await send_message(
            update, context,
            "Ошибка: Неверное действие. Вернитесь в главное меню.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
            ])
        )
        return WELCOME_STATE

    elif query.data == "history":
        configs = load_configs()
        user_configs = configs.get(str(user_id), {})
        if not user_configs:
            await send_message(
                update, context,
                "⏳ У вас пока нет сохранённых конфигураций. Создайте свою первую конфигурацию для расчета параметров БПЛА!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
                ])
            )
            return WELCOME_STATE

        keyboard = [
            [InlineKeyboardButton(f"{name} ({data['created_at']})", callback_data=f"config_{name}")]
            for name, data in user_configs.items()
        ]
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        await send_message(update, context, "📜 Выберите конфигурацию из списка:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SHOW_HISTORY

    elif query.data == "new_config":
        keyboard = [
            [InlineKeyboardButton("Барражирующий БВС", callback_data="loitering")],
            [InlineKeyboardButton("БВС дальнего действия", callback_data="long_range")]
        ]
        sent_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="Выберите тип БВС:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data['message_ids'].append(sent_msg.message_id)
        logger.info(f"Пользователь {user_id} выбрал новую конфигурацию, отправлено сообщение {sent_msg.message_id}")
        return CHOOSE_TYPE

    elif query.data == "back_to_welcome":
        welcome_text = """
🚀 *DroneDesigner* — Telegram-бот для расчёта параметров БПЛА

• Масса конструкции
• Требуемая мощность
• Параметры батареи
• Размах и площадь крыла
• Практический потолок и плотность воздуха

Для инженеров и энтузиастов БПЛА!
        """
        await send_message(
            update, context,
            welcome_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 История", callback_data="history")],
                [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")]
            ]),
            parse_mode="Markdown"
        )
        return WELCOME_STATE

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора конфигурации из истории"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора конфигурации")

    await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])

    if query.data == "back_to_welcome":
        welcome_text = """
🚀 *DroneDesigner* — Telegram-бот для расчёта параметров БПЛА

• Масса конструкции
• Требуемая мощность
• Параметры батареи
• Размах и площадь крыла
• Практический потолок и плотность воздуха

Для инженеров и энтузиастов БПЛА!
        """
        await send_message(
            update, context,
            welcome_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 История", callback_data="history")],
                [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")]
            ]),
            parse_mode="Markdown"
        )
        return WELCOME_STATE

    elif query.data == "back_to_current":
        data = context.user_data.get('current_config', {})
        if not data:
            await send_message(
                update, context,
                "⚠️ Текущая конфигурация не найдена. Начните новый расчёт.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛠 Новый расчёт", callback_data="restart")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
                ])
            )
            return CALCULATE

        result_text = f"""
📊 Результаты расчета:

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} вернулся к текущей конфигурации")
        return CALCULATE

    if match := re.match(r"config_(.+)", query.data):
        config_name = match.group(1)
        configs = load_configs()
        config = configs.get(str(user_id), {}).get(config_name)
        if not config:
            await send_message(
                update, context,
                "⚠️ Конфигурация не найдена. Вернитесь в главное меню.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Назад", callback_data="history")]
                ])
            )
            return SHOW_HISTORY

        result_text = f"""
📊 Конфигурация: {config_name} ({config['created_at']})

🔹 Взлетная масса: {config['takeoff_mass']:.2f} кг
🔹 Тяга: {config['thrust_cruise']:.2f} кгс (крейсер), {config['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {config['power_cruise']/1000:.2f} кВт (крейсер), {config['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {config['ceiling']:.0f} м
🔹 Плотность воздуха: {config['air_density']:.3f} кг/м³
🔹 Размах крыла: {config['wingspan']:.2f} м
🔹 Площадь крыла: {config['wing_area']:.2f} м²

🔋 Аккумулятор {config['battery_type']}:
- Масса: {config['battery_mass']:.2f} кг
- Напряжение: {config['battery_voltage']} В
- Емкость: {config['battery_capacity_ah']:.2f} А·ч (рекомендуется {config['battery_capacity_recommended']} А·ч)

✈️ Параметры полета:
- Дальность: {config.get('distance', 0):.2f} км
- Время: {config.get('flight_time', 0):.2f} ч
- Скорость: {config.get('speed', 0)} км/ч
- Маневры: {config.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {config['battery_info']}
- Электромотор: {config['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("⬅ Назад к списку", callback_data="history")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{config_name}")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} просмотрел конфигурацию {config_name}")
        return SHOW_CONFIG

async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка действий с конфигурацией"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для действий с конфигурацией")

    await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])

    if query.data == "history":
        configs = load_configs()
        user_configs = configs.get(str(user_id), {})
        if not user_configs:
            await send_message(
                update, context,
                "⏳ У вас пока нет сохранённых конфигураций. Создайте свою первую конфигурацию для расчета параметров БПЛА!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
                ])
            )
            return WELCOME_STATE

        keyboard = [
            [InlineKeyboardButton(f"{name} ({data['created_at']})", callback_data=f"config_{name}")]
            for name, data in user_configs.items()
        ]
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        await send_message(update, context, "📜 Выберите конфигурацию из списка:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SHOW_HISTORY

    if match := re.match(r"delete_(.+)", query.data):
        config_name = match.group(1)
        await send_message(
            update, context,
            "Вы точно хотите удалить конфигурацию?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Удалить", callback_data=f"confirm_delete_{config_name}")],
                [InlineKeyboardButton("🚫 Отмена", callback_data=f"config_{config_name}")]
            ])
        )
        return CONFIRM_DELETE

    if match := re.match(r"config_(.+)", query.data):
        config_name = match.group(1)
        configs = load_configs()
        config = configs.get(str(user_id), {}).get(config_name)
        if not config:
            await send_message(
                update, context,
                "⚠️ Конфигурация не найдена. Вернитесь в главное меню.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Назад", callback_data="history")]
                ])
            )
            return SHOW_HISTORY

        result_text = f"""
📊 Конфигурация: {config_name} ({config['created_at']})

🔹 Взлетная масса: {config['takeoff_mass']:.2f} кг
🔹 Тяга: {config['thrust_cruise']:.2f} кгс (крейсер), {config['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {config['power_cruise']/1000:.2f} кВт (крейсер), {config['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {config['ceiling']:.0f} м
🔹 Плотность воздуха: {config['air_density']:.3f} кг/м³
🔹 Размах крыла: {config['wingspan']:.2f} м
🔹 Площадь крыла: {config['wing_area']:.2f} м²

🔋 Аккумулятор {config['battery_type']}:
- Масса: {config['battery_mass']:.2f} кг
- Напряжение: {config['battery_voltage']} В
- Емкость: {config['battery_capacity_ah']:.2f} А·ч (рекомендуется {config['battery_capacity_recommended']} А·ч)

✈️ Параметры полета:
- Дальность: {config.get('distance', 0):.2f} км
- Время: {config.get('flight_time', 0):.2f} ч
- Скорость: {config.get('speed', 0)} км/ч
- Маневры: {config.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {config['battery_info']}
- Электромотор: {config['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("⬅ Назад к списку", callback_data="history")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{config_name}")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} просмотрел конфигурацию {config_name}")
        return SHOW_CONFIG

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка подтверждения удаления конфигурации"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для подтверждения удаления")

    await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])

    if match := re.match(r"confirm_delete_(.+)", query.data):
        config_name = match.group(1)
        configs = load_configs()
        if str(user_id) in configs and config_name in configs[str(user_id)]:
            del configs[str(user_id)][config_name]
            if not configs[str(user_id)]:
                del configs[str(user_id)]
            save_configs(configs)
            if os.getenv('RENDER'):
                update_repo()
            logger.info(f"Пользователь {user_id} удалил конфигурацию {config_name}")
        
        user_configs = configs.get(str(user_id), {})
        if not user_configs:
            await send_message(
                update, context,
                "⏳ У вас пока нет сохранённых конфигураций. Создайте свою первую конфигурацию для расчета параметров БПЛА!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
                ])
            )
            return WELCOME_STATE

        keyboard = [
            [InlineKeyboardButton(f"{name} ({data['created_at']})", callback_data=f"config_{name}")]
            for name, data in user_configs.items()
        ]
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        await send_message(update, context, "📜 Конфигурация удалена. Выберите другую конфигурацию:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SHOW_HISTORY

    if match := re.match(r"config_(.+)", query.data):
        config_name = match.group(1)
        configs = load_configs()
        config = configs.get(str(user_id), {}).get(config_name)
        if not config:
            await send_message(
                update, context,
                "⚠️ Конфигурация не найдена. Вернитесь в главное меню.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Назад", callback_data="history")]
                ])
            )
            return SHOW_HISTORY

        result_text = f"""
📊 Конфигурация: {config_name} ({config['created_at']})

🔹 Взлетная масса: {config['takeoff_mass']:.2f} кг
🔹 Тяга: {config['thrust_cruise']:.2f} кгс (крейсер), {config['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {config['power_cruise']/1000:.2f} кВт (крейсер), {config['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {config['ceiling']:.0f} м
🔹 Плотность воздуха: {config['air_density']:.3f} кг/м³
🔹 Размах крыла: {config['wingspan']:.2f} м
🔹 Площадь крыла: {config['wing_area']:.2f} м²

🔋 Аккумулятор {config['battery_type']}:
- Масса: {config['battery_mass']:.2f} кг
- Напряжение: {config['battery_voltage']} В
- Емкость: {config['battery_capacity_ah']:.2f} А·ч (рекомендуется {config['battery_capacity_recommended']} А·ч)

✈️ Параметры полета:
- Дальность: {config.get('distance', 0):.2f} км
- Время: {config.get('flight_time', 0):.2f} ч
- Скорость: {config.get('speed', 0)} км/ч
- Маневры: {config.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {config['battery_info']}
- Электромотор: {config['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("⬅ Назад к списку", callback_data="history")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{config_name}")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} просмотрел конфигурацию {config_name}")
        return SHOW_CONFIG

async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора типа БВС"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []

    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора типа БВС")

    if query.data not in ["loitering", "long_range"]:
        keyboard = [
            [InlineKeyboardButton("Барражирующий БВС", callback_data="loitering")],
            [InlineKeyboardButton("БВС дальнего действия", callback_data="long_range")]
        ]
        sent_msg = await send_message(
            update, context,
            "Ошибка: Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_TYPE
    
    await delete_messages(context, chat_id, keep_ids=[])
    
    context.user_data['type'] = query.data
    logger.info(f"Пользователь {user_id} выбрал тип БВС: {query.data}")
    
    prompt = ("Введите время полета в часах (например: 2.5). Это общее время, которое БПЛА должен находиться в воздухе:" 
              if query.data == "loitering" 
              else "Введите дальность полета в км (например: 300). Это расстояние, которое БПЛА должен преодолеть:")
    
    sent_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=prompt,
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data['message_ids'].append(sent_msg.message_id)
    logger.info(f"Отправлено сообщение '{prompt[:50]}...' с ID {sent_msg.message_id} для пользователя {user_id}")
    
    return INPUT_FLIGHT_TIME

async def input_flight_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода времени полета или дальности"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для ввода времени полета")
    
    try:
        value = float(update.message.text.replace(',', '.'))
        if value <= 0:
            raise ValueError
        
        context.user_data['flight_time' if context.user_data['type'] == "loitering" else 'distance'] = value
        prompt_msg = await send_message(
            update, context,
            "Введите крейсерскую скорость в км/ч (например: 120):",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса скорости")
        logger.debug(f"Текущее состояние message_ids после ввода времени: {context.user_data['message_ids']}")
        return INPUT_SPEED
        
    except ValueError:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Введите положительное число:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода")
        logger.debug(f"Текущее состояние message_ids после ошибки: {context.user_data['message_ids']}")
        return INPUT_FLIGHT_TIME

async def input_speed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода скорости"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для ввода скорости")
    
    try:
        speed = float(update.message.text.replace(',', '.'))
        if speed <= 0:
            raise ValueError
            
        context.user_data['speed'] = speed
        
        if context.user_data['type'] == "loitering":
            context.user_data['distance'] = context.user_data['flight_time'] * speed
        else:
            context.user_data['flight_time'] = context.user_data['distance'] / speed
            
        prompt_msg = await send_message(
            update, context,
            "Введите массу полезной нагрузки в кг (например: 2.5):",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса массы полезной нагрузки")
        logger.debug(f"Текущее состояние message_ids после ввода скорости: {context.user_data['message_ids']}")
        logger.info(f"Пользователь {user_id} ввел скорость: {speed}")
        return INPUT_PAYLOAD
        
    except ValueError:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Введите положительное число:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода")
        logger.debug(f"Текущее состояние message_ids после ошибки: {context.user_data['message_ids']}")
        return INPUT_SPEED

async def input_payload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода массы полезной нагрузки"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для ввода массы полезной нагрузки")
    
    try:
        payload = float(update.message.text.replace(',', '.'))
        if payload <= 0:
            raise ValueError
            
        context.user_data['payload'] = payload
        
        aero_info = """✈️ Выберите аэродинамическое качество:

6 - Самолет слабо оптимизированной аэродинамической формы:
- Квадратный в поперечном сечении фюзеляж
- Открытые участки, нет капота двигателя
- Прямое крыло без законцовок
- Неубираемые шасси
- Самый простой вариант для реализации

8 - Базовая оптимизация:
- Квадратный в поперечном сечении фюзеляж, но обшитый
- Двигатель закрыт капотом
- Прямое крыло без законцовок
- Неубираемые шасси

12 - Продвинутая оптимизация:
- Скругленный в поперечном сечении фюзеляж
- Трапециевидное крыло с законцовками
- Двигатель закрыт капотом
- Неубираемые шасси

14 - Высокая оптимизация:
- Оптимальная аэродинамика
- Убираемые шасси
- Все элементы закрыты обтекателями
- Самый сложный вариант для реализации"""

        keyboard = [
            [InlineKeyboardButton("6 (Плохое)", callback_data="6")],
            [InlineKeyboardButton("8 (Среднее)", callback_data="8")],
            [InlineKeyboardButton("12 (Хорошее)", callback_data="12")],
            [InlineKeyboardButton("14 (Отличное)", callback_data="14")]
        ]
        
        prompt_msg = await send_message(update, context, aero_info, reply_markup=InlineKeyboardMarkup(keyboard))
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса аэродинамического качества")
        logger.debug(f"Текущее состояние message_ids после ввода массы: {context.user_data['message_ids']}")
        logger.info(f"Пользователь {user_id} ввел массу полезной нагрузки: {payload}")
        return INPUT_AERO_QUALITY
        
    except ValueError:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Введите положительное число:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода")
        logger.debug(f"Текущее состояние message_ids после ошибки: {context.user_data['message_ids']}")
        return INPUT_PAYLOAD

async def input_aero_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора аэродинамического качества"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора аэродинамического качества")
    
    if query.data not in SELECTION_MAPS['aero_quality']:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("6 (Плохое)", callback_data="6")],
                [InlineKeyboardButton("8 (Среднее)", callback_data="8")],
                [InlineKeyboardButton("12 (Хорошее)", callback_data="12")],
                [InlineKeyboardButton("14 (Отличное)", callback_data="14")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return INPUT_AERO_QUALITY
    
    context.user_data['aero_quality'] = int(query.data)
    
    thrust_info = """🚀 Выберите запас по тяге:

1.5 - Маневры с креном до 45° (рекомендуется)
- Минимальный рост взлетной массы
- Подходит для большинства задач

2.0 - Маневры с креном до 65°
- Умеренный рост массы
- Для сложных маневров

3.0 - Пилотажные маневры (крен до 80°)
- Сильный рост массы
- Только для специальных задач"""
    
    keyboard = [
        [InlineKeyboardButton("1.5 (до 45°)", callback_data="1.5")],
        [InlineKeyboardButton("2.0 (до 65°)", callback_data="2.0")],
        [InlineKeyboardButton("3.0 (пилотаж)", callback_data="3.0")]
    ]
    
    prompt_msg = await send_message(update, context, thrust_info, reply_markup=InlineKeyboardMarkup(keyboard))
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса запаса по тяге")
    logger.debug(f"Текущее состояние message_ids после выбора аэродинамики: {context.user_data['message_ids']}")
    logger.info(f"Пользователь {user_id} выбрал аэродинамическое качество: {query.data}")
    return INPUT_THRUST_RESERVE

async def input_thrust_reserve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора запаса по тяге"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора запаса по тяге")
    
    if query.data not in SELECTION_MAPS['thrust_reserve']:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1.5 (до 45°)", callback_data="1.5")],
                [InlineKeyboardButton("2.0 (до 65°)", callback_data="2.0")],
                [InlineKeyboardButton("3.0 (пилотаж)", callback_data="3.0")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return INPUT_THRUST_RESERVE
    
    context.user_data['thrust_reserve'] = float(query.data)
    
    maneuver_info = """🔄 Выберите % времени для активного маневрирования:

10-15% - Основной полет без крена
- Маневры только для взлета/посадки и на подлете к цели
- Минимальное влияние на массу

30% - Барражирование над точкой
- Постоянные маневры с креном
- Умеренный рост массы"""
        
    keyboard = [
        [InlineKeyboardButton("10%", callback_data="10")],
        [InlineKeyboardButton("15%", callback_data="15")],
        [InlineKeyboardButton("30%", callback_data="30")]
    ]
    
    prompt_msg = await send_message(update, context, maneuver_info, reply_markup=InlineKeyboardMarkup(keyboard))
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса времени маневрирования")
    logger.debug(f"Текущее состояние message_ids после выбора запаса по тяге: {context.user_data['message_ids']}")
    logger.info(f"Пользователь {user_id} выбрал запас по тяге: {query.data}")
    return INPUT_MANEUVER_TIME

async def input_maneuver_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора времени маневрирования"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора времени маневрирования")
    
    if query.data not in ["10", "15", "30"]:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("10%", callback_data="10")],
                [InlineKeyboardButton("15%", callback_data="15")],
                [InlineKeyboardButton("30%", callback_data="30")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return INPUT_MANEUVER_TIME
    
    context.user_data['maneuver_time'] = float(query.data)
    
    flight_time = context.user_data.get('flight_time', 0)
    battery_type = "Li-ion (300 Вт·ч/кг)" if flight_time > 1 else "LiPo (200 Вт·ч/кг)"
    context.user_data['battery_capacity'] = 300 if flight_time > 1 else 200
    
    keyboard = [
        [InlineKeyboardButton("Композитные материалы (0.40)", callback_data="0.40")],
        [InlineKeyboardButton("Цельнометаллические (0.45)", callback_data="0.45")],
        [InlineKeyboardButton("Дерево/фанера (0.50)", callback_data="0.50")]
    ]
    
    prompt_msg = await send_message(
        update, context,
        f"🔋 Выбран аккумулятор: {battery_type}\n\nВыберите материал планера:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса материала планера")
    logger.debug(f"Текущее состояние message_ids после выбора времени маневрирования: {context.user_data['message_ids']}")
    logger.info(f"Пользователь {user_id} выбрал время маневрирования: {query.data}%")
    return INPUT_PLANE_MATERIAL

async def input_plane_material(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора материала планера"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора материала планера")
    
    if query.data not in SELECTION_MAPS['plane_material']:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Композитные материалы (0.40)", callback_data="0.40")],
                [InlineKeyboardButton("Цельнометаллические (0.45)", callback_data="0.45")],
                [InlineKeyboardButton("Дерево/фанера (0.50)", callback_data="0.50")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return INPUT_PLANE_MATERIAL
    
    context.user_data['plane_mass'] = float(query.data)
    
    propeller_info = """🌀 Выберите тип винта:

Стандартные винты - КПД 75%
- Обычные серийные винты
- Дешевле и доступнее

Винты на заказ - КПД 80%
- Оптимизированы под вашу конструкцию
- Дороже, но эффективнее"""
        
    keyboard = [
        [InlineKeyboardButton("Стандартные винты (75%)", callback_data="0.75")],
        [InlineKeyboardButton("Винты на заказ (80%)", callback_data="0.80")]
    ]
    
    prompt_msg = await send_message(update, context, propeller_info, reply_markup=InlineKeyboardMarkup(keyboard))
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса типа винта")
    logger.debug(f"Текущее состояние message_ids после выбора материала: {context.user_data['message_ids']}")
    logger.info(f"Пользователь {user_id} выбрал материал планера: {query.data}")
    return INPUT_PROPELLER_TYPE

async def input_propeller_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора типа винта"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора типа винта")
    
    if query.data not in SELECTION_MAPS['propeller_eff']:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Стандартные винты (75%)", callback_data="0.75")],
                [InlineKeyboardButton("Винты на заказ (80%)", callback_data="0.80")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return INPUT_PROPELLER_TYPE
    
    context.user_data['propeller_eff'] = float(query.data)
    
    takeoff_info = """🛫 Выберите тип взлета:
    
С катапульты - коэффициент 0.3
- Минимальные требования к тяге

С бетонной ВПП - коэффициент 0.4
- Стандартный взлет

С грунтовой ВПП - коэффициент 0.6
- Требуется повышенная тяга"""
        
    keyboard = [
        [InlineKeyboardButton("С катапульты (0.3)", callback_data="0.3")],
        [InlineKeyboardButton("С бетонной ВПП (0.4)", callback_data="0.4")],
        [InlineKeyboardButton("С грунтовой ВПП (0.6)", callback_data="0.6")]
    ]
    
    prompt_msg = await send_message(update, context, takeoff_info, reply_markup=InlineKeyboardMarkup(keyboard))
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса типа взлета")
    logger.debug(f"Текущее состояние message_ids после выбора типа винта: {context.user_data['message_ids']}")
    logger.info(f"Пользователь {user_id} выбрал тип винта: {query.data}")
    return INPUT_TAKEOFF_TYPE

async def input_takeoff_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора типа взлета"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для выбора типа взлета")
    
    if query.data not in SELECTION_MAPS['takeoff_type']:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("С катапульты (0.3)", callback_data="0.3")],
                [InlineKeyboardButton("С бетонной ВПП (0.4)", callback_data="0.4")],
                [InlineKeyboardButton("С грунтовой ВПП (0.6)", callback_data="0.6")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return INPUT_TAKEOFF_TYPE
    
    context.user_data['takeoff_type'] = float(query.data)
    
    prompt_msg = await send_message(
        update, context,
        "Введите практический потолок полета в метрах (например, 5000):",
        reply_markup=ReplyKeyboardRemove()
    )
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса высоты")
    logger.debug(f"Текущее состояние message_ids после выбора типа взлета: {context.user_data['message_ids']}")
    logger.info(f"Пользователь {user_id} выбрал тип взлета: {query.data}")
    return INPUT_CEILING

async def input_ceiling(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода практического потолка полета"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для ввода высоты")
    
    try:
        ceiling = float(update.message.text.replace(',', '.'))
        if ceiling < 0 or ceiling > 15000:
            raise ValueError("Высота должна быть от 0 до 15000 м")
            
        context.user_data['ceiling'] = ceiling
        data = calculate_results(context)
        context.user_data['current_config'] = data
        
        result_text = f"""
📊 Результаты расчета:

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
        ]
        
        prompt_msg = await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для отображения результатов")
        logger.debug(f"Текущее состояние message_ids после ввода высоты: {context.user_data['message_ids']}")
        logger.info(f"Пользователь {user_id} ввел практический потолок: {ceiling} м")
        return CALCULATE
        
    except ValueError as e:
        prompt_msg = await send_message(
            update, context,
            f"Ошибка! {str(e)}. Введите число от 0 до 15000:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода")
        logger.debug(f"Текущее состояние message_ids после ошибки: {context.user_data['message_ids']}")
        return INPUT_CEILING

def calculate_results(context):
    """Расчет параметров БПЛА"""
    data = context.user_data
    g = 9.81  # м/с²
    
    # Основные параметры
    payload = data['payload']
    plane_mass_coeff = data['plane_mass']
    aero_quality = data['aero_quality']
    thrust_reserve = data['thrust_reserve']
    maneuver_time = data['maneuver_time'] / 100
    speed_kmh = data['speed']
    speed_ms = speed_kmh / 3.6
    propeller_eff = data['propeller_eff']
    takeoff_coeff = data['takeoff_type']
    flight_time_h = data['flight_time']
    ceiling = data.get('ceiling', 0)  # Практический потолок, м
    
    # Расчет плотности воздуха
    air_density = calculate_air_density(ceiling)
    
    # Расчет взлетной массы
    takeoff_mass = payload / (1 - plane_mass_coeff)
    
    # Расчет подъемной силы и площади крыла
    lift = takeoff_mass * g
    C_L = 1.0  # Предполагаемый коэффициент подъемной силы
    wing_area = lift / (0.5 * air_density * speed_ms**2 * C_L)
    
    # Удлинение крыла в зависимости от аэродинамического качества
    aspect_ratio_map = {6: 6, 8: 7, 12: 8, 14: 9}
    aspect_ratio = aspect_ratio_map[aero_quality]
    
    # Расчет размаха крыла
    wingspan = (wing_area * aspect_ratio) ** 0.5
    
    # Расчет тяги
    thrust_cruise = takeoff_mass * g / aero_quality
    thrust_max = thrust_cruise * thrust_reserve
    thrust_takeoff = thrust_max * takeoff_coeff
    
    # Расчет мощности
    power_cruise = thrust_cruise * speed_ms / propeller_eff
    power_max = thrust_max * speed_ms / propeller_eff
    
    # Расчет батареи
    battery_capacity = data['battery_capacity']
    battery_voltage = 48 if battery_capacity == 300 else 36
    energy_required = power_cruise * flight_time_h * 3600 * (1 + maneuver_time * (thrust_reserve - 1))
    battery_capacity_ah = energy_required / (battery_voltage * 3600)
    battery_mass = energy_required / (battery_capacity * 3600)
    
    # Сохранение результатов
    data.update({
        'takeoff_mass': takeoff_mass,
        'thrust_cruise': thrust_cruise / g,
        'thrust_max': thrust_max / g,
        'power_cruise': power_cruise,
        'power_max': power_max,
        'battery_mass': battery_mass,
        'battery_voltage': battery_voltage,
        'battery_capacity_ah': battery_capacity_ah,
        'battery_capacity_recommended': battery_capacity_ah * 1.2,
        'battery_type': "Li-ion" if flight_time_h > 1 else "LiPo",
        'battery_info': f"{battery_capacity_ah:.2f} А·ч ({battery_voltage} В, {battery_mass:.2f} кг)",
        'rotor_info': f"{power_max/1000:.2f} кВт, {thrust_max/g:.2f} кгс",
        'wing_area': wing_area,
        'wingspan': wingspan,
        'air_density': air_density,
        'ceiling': ceiling
    })
    return data

async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка действий после расчета"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для обработки расчета")
    
    if query.data == "restart":
        context.user_data.clear()
        context.user_data['message_ids'] = []
        keyboard = [
            [InlineKeyboardButton("Барражирующий БВС", callback_data="loitering")],
            [InlineKeyboardButton("БВС дальнего действия", callback_data="long_range")]
        ]
        await send_message(
            update, context,
            "Выберите тип БВС:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CHOOSE_TYPE
    
    if query.data == "change_params":
        keyboard = [
            [InlineKeyboardButton("Время полета/Дальность", callback_data="change_flight_time")],
            [InlineKeyboardButton("Крейсерская скорость", callback_data="change_speed")],
            [InlineKeyboardButton("Аэродинамическое качество", callback_data="change_aero_quality")],
            [InlineKeyboardButton("Время маневрирования", callback_data="change_maneuver_time")],
            [InlineKeyboardButton("⬅ Назад", callback_data="back_to_current")]
        ]
        await send_message(
            update, context,
            "Выберите параметр для изменения:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CALCULATE
    
    if query.data == "change_flight_time":
        prompt = ("Введите новое время полета в часах (например: 2.5):" 
                  if context.user_data['type'] == "loitering" 
                  else "Введите новую дальность полета в км (например: 300):")
        await send_message(
            update, context,
            prompt,
            reply_markup=ReplyKeyboardRemove()
        )
        return CHANGE_FLIGHT_TIME
    
    if query.data == "change_speed":
        await send_message(
            update, context,
            "Введите новую крейсерскую скорость в км/ч (например: 120):",
            reply_markup=ReplyKeyboardRemove()
        )
        return CHANGE_SPEED
    
    if query.data == "change_aero_quality":
        keyboard = [
            [InlineKeyboardButton("6 (Плохое)", callback_data="6")],
            [InlineKeyboardButton("8 (Среднее)", callback_data="8")],
            [InlineKeyboardButton("12 (Хорошее)", callback_data="12")],
            [InlineKeyboardButton("14 (Отличное)", callback_data="14")]
        ]
        await send_message(
            update, context,
            "Выберите новое аэродинамическое качество:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CHANGE_AERO_QUALITY
    
    if query.data == "change_maneuver_time":
        keyboard = [
            [InlineKeyboardButton("10%", callback_data="10")],
            [InlineKeyboardButton("15%", callback_data="15")],
            [InlineKeyboardButton("30%", callback_data="30")]
        ]
        await send_message(
            update, context,
            "Выберите новый % времени для активного маневрирования:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CHANGE_MANEUVER_TIME
    
    if query.data == "save_config":
        await send_message(
            update, context,
            "Введите название конфигурации (например: Drone_1):",
            reply_markup=ReplyKeyboardRemove()
        )
        return INPUT_CONFIG_NAME
    
    if query.data == "history":
        configs = load_configs()
        user_configs = configs.get(str(user_id), {})
        if not user_configs:
            await send_message(
                update, context,
                "⏳ У вас пока нет сохранённых конфигураций. Создайте свою первую конфигурацию для расчета параметров БПЛА!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
                ])
            )
            return WELCOME_STATE

        keyboard = [
            [InlineKeyboardButton(f"{name} ({data['created_at']})", callback_data=f"config_{name}")]
            for name, data in user_configs.items()
        ]
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        await send_message(update, context, "📜 Выберите конфигурацию из списка:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SHOW_HISTORY

    if query.data == "back_to_welcome":
        welcome_text = """
🚀 *DroneDesigner* — Telegram-бот для расчёта параметров БПЛА

• Масса конструкции
• Требуемая мощность
• Параметры батареи
• Размах и площадь крыла
• Практический потолок и плотность воздуха

Для инженеров и энтузиастов БПЛА!
        """
        await send_message(
            update, context,
            welcome_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 История", callback_data="history")],
                [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")]
            ]),
            parse_mode="Markdown"
        )
        return WELCOME_STATE

    if query.data == "back_to_current":
        data = context.user_data.get('current_config', {})
        if not data:
            await send_message(
                update, context,
                "⚠️ Текущая конфигурация не найдена. Начните новый расчёт.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛠 Новый расчёт", callback_data="restart")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
                ])
            )
            return CALCULATE

        result_text = f"""
📊 Результаты расчета:

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return CALCULATE

async def change_flight_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка изменения времени полета или дальности"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    try:
        value = float(update.message.text.replace(',', '.'))
        if value <= 0:
            raise ValueError
        
        if context.user_data['type'] == "loitering":
            context.user_data['flight_time'] = value
            context.user_data['distance'] = value * context.user_data['speed']
        else:
            context.user_data['distance'] = value
            context.user_data['flight_time'] = value / context.user_data['speed']
        
        data = calculate_results(context)
        context.user_data['current_config'] = data
        
        result_text = f"""
📊 Результаты расчета:

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} изменил время полета/дальность: {value}")
        return CALCULATE
        
    except ValueError:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Введите положительное число:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода")
        return CHANGE_FLIGHT_TIME

async def change_speed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка изменения скорости"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    try:
        speed = float(update.message.text.replace(',', '.'))
        if speed <= 0:
            raise ValueError
            
        context.user_data['speed'] = speed
        
        if context.user_data['type'] == "loitering":
            context.user_data['distance'] = context.user_data['flight_time'] * speed
        else:
            context.user_data['flight_time'] = context.user_data['distance'] / speed
        
        data = calculate_results(context)
        context.user_data['current_config'] = data
        
        result_text = f"""
📊 Результаты расчета:

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
        """
        keyboard = [
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} изменил скорость: {speed} км/ч")
        return CALCULATE
        
    except ValueError:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Введите положительное число:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода")
        return CHANGE_SPEED

async def change_aero_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка изменения аэродинамического качества"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для изменения аэродинамического качества")
    
    if query.data not in SELECTION_MAPS['aero_quality']:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("6 (Плохое)", callback_data="6")],
                [InlineKeyboardButton("8 (Среднее)", callback_data="8")],
                [InlineKeyboardButton("12 (Хорошее)", callback_data="12")],
                [InlineKeyboardButton("14 (Отличное)", callback_data="14")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return CHANGE_AERO_QUALITY
    
    context.user_data['aero_quality'] = int(query.data)
    data = calculate_results(context)
    context.user_data['current_config'] = data
    
    result_text = f"""
📊 Результаты расчета:

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
    """
    keyboard = [
        [InlineKeyboardButton("📖 История", callback_data="history")],
        [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
        [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
    ]
    await send_message(
        update, context,
        result_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    logger.info(f"Пользователь {user_id} изменил аэродинамическое качество: {query.data}")
    return CALCULATE

async def change_maneuver_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка изменения времени маневрирования"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для изменения времени маневрирования")
    
    if query.data not in ["10", "15", "30"]:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("10%", callback_data="10")],
                [InlineKeyboardButton("15%", callback_data="15")],
                [InlineKeyboardButton("30%", callback_data="30")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке выбора")
        return CHANGE_MANEUVER_TIME
    
    context.user_data['maneuver_time'] = float(query.data)
    data = calculate_results(context)
    context.user_data['current_config'] = data
    
    result_text = f"""
📊 Результаты расчета:

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
    """
    keyboard = [
        [InlineKeyboardButton("📖 История", callback_data="history")],
        [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
        [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
    ]
    await send_message(
        update, context,
        result_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    logger.info(f"Пользователь {user_id} изменил время маневрирования: {query.data}%")
    return CALCULATE

async def save_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохранение конфигурации"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    config_name = update.message.text.strip()
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для сохранения конфигурации")
    
    if not config_name or len(config_name) > 50:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Название должно быть непустым и не длиннее 50 символов. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Назад", callback_data="back_to_current")]
            ])
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода названия")
        return INPUT_CONFIG_NAME
    
    configs = load_configs()
    if str(user_id) not in configs:
        configs[str(user_id)] = {}
    
    data = context.user_data['current_config'].copy()
    data['created_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    configs[str(user_id)][config_name] = data
    save_configs(configs)
    
    if os.getenv('RENDER'):
        update_repo()
    
    result_text = f"""
📊 Конфигурация сохранена как: {config_name}

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)
🔹 Практический потолок: {data['ceiling']:.0f} м
🔹 Плотность воздуха: {data['air_density']:.3f} кг/м³
🔹 Размах крыла: {data['wingspan']:.2f} м
🔹 Площадь крыла: {data['wing_area']:.2f} м²

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data.get('distance', 0):.2f} км
- Время: {data.get('flight_time', 0):.2f} ч
- Скорость: {data.get('speed', 0)} км/ч
- Маневры: {data.get('maneuver_time', 0)}% времени

🦾 Комплектация:
- АКБ: {data['battery_info']}
- Электромотор: {data['rotor_info']}
    """
    keyboard = [
        [InlineKeyboardButton("📖 История", callback_data="history")],
        [InlineKeyboardButton("🛠 Новый расчёт", callback_data="restart")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")]
    ]
    await send_message(
        update, context,
        result_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    logger.info(f"Пользователь {user_id} сохранил конфигурацию: {config_name}")
    return CALCULATE

def main():
    """Запуск бота"""
    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            WELCOME_STATE: [CallbackQueryHandler(handle_welcome)],
            SHOW_HISTORY: [CallbackQueryHandler(show_history)],
            SHOW_CONFIG: [CallbackQueryHandler(show_config)],
            CONFIRM_DELETE: [CallbackQueryHandler(confirm_delete)],
            CHOOSE_TYPE: [CallbackQueryHandler(choose_type)],
            INPUT_FLIGHT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_flight_time)],
            INPUT_SPEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_speed)],
            INPUT_PAYLOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_payload)],
            INPUT_AERO_QUALITY: [CallbackQueryHandler(input_aero_quality)],
            INPUT_THRUST_RESERVE: [CallbackQueryHandler(input_thrust_reserve)],
            INPUT_MANEUVER_TIME: [CallbackQueryHandler(input_maneuver_time)],
            INPUT_PLANE_MATERIAL: [CallbackQueryHandler(input_plane_material)],
            INPUT_PROPELLER_TYPE: [CallbackQueryHandler(input_propeller_type)],
            INPUT_TAKEOFF_TYPE: [CallbackQueryHandler(input_takeoff_type)],
            INPUT_CEILING: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_ceiling)],
            CALCULATE: [CallbackQueryHandler(calculate)],
            CHANGE_FLIGHT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_flight_time)],
            CHANGE_SPEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_speed)],
            CHANGE_AERO_QUALITY: [CallbackQueryHandler(change_aero_quality)],
            CHANGE_MANEUVER_TIME: [CallbackQueryHandler(change_maneuver_time)],
            INPUT_CONFIG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_config)]
        },
        fallbacks=[CommandHandler('start', start)]
    )
    
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()
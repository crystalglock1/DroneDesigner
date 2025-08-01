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
    CALCULATE, CHANGE_FLIGHT_TIME, CHANGE_SPEED,
    CHANGE_AERO_QUALITY, CHANGE_MANEUVER_TIME, WELCOME_STATE,
    SHOW_HISTORY, SHOW_CONFIG, CONFIRM_DELETE, INPUT_CONFIG_NAME
) = range(20)

# Токен бота из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
load_dotenv()
GIT_TOKEN = os.getenv("GIT_TOKEN")
CONFIG_FILE = 'configurations.json'

# Словарь для маппинга выбора
SELECTION_MAPS = {
    'aero_quality': {"6": 6, "8": 8, "12": 12, "14": 14},
    'thrust_reserve': {"1.5": 1.5, "2.0": 2.0, "3.0": 3.0},
    'plane_material': {"0.40": 0.40, "0.45": 0.45, "0.50": 0.50},
    'propeller_eff': {"0.75": 0.75, "0.80": 0.80},
    'takeoff_type': {"0.3": 0.3, "0.4": 0.4, "0.6": 0.6}
}

def load_configs():
    """Загрузка конфигураций из JSON-файла"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        return {}
    except json.JSONDecodeError:
        logger.error("Ошибка чтения configurations.json, возвращается пустой словарь")
        return {}

def save_configs(configs):
    """Сохранение конфигураций в JSON-файл"""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(configs, f, indent=4)
    except Exception as e:
        logger.error(f"Ошибка записи в configurations.json: {e}")

def update_repo():
    """Обновление репозитория GitHub"""
    try:
        subprocess.run(['git', 'config', '--global', 'user.email', 'bot@example.com'], check=True)
        subprocess.run(['git', 'config', '--global', 'user.name', 'Bot'], check=True)
        subprocess.run(['git', 'add', CONFIG_FILE], check=True)
        subprocess.run(['git', 'commit', '-m', 'Обновлены конфигурации'], check=True)
        subprocess.run(['git', 'push', 'origin', 'main'], check=True)
        logger.info("Конфигурации успешно отправлены в репозиторий")
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка при пушe в репозиторий: {e}")

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
• И другие ключевые характеристики

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
• И другие ключевые характеристики

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
• И другие ключевые характеристики

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

        result_text = f"""📊 Конфигурация: {config_name} ({config['created_at']})

🔹 Взлетная масса: {config['takeoff_mass']:.2f} кг
🔹 Тяга: {config['thrust_cruise']:.2f} кгс (крейсер), {config['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {config['power_cruise']/1000:.2f} кВт (крейсер), {config['power_max']/1000:.2f} кВт (макс)

🔋 Аккумулятор {config['battery_type']}:
- Масса: {config['battery_mass']:.2f} кг
- Напряжение: {config['battery_voltage']} В
- Емкость: {config['battery_capacity_ah']:.2f} А·ч (рекомендуется {config['battery_capacity_recommended']} А·ч)

✈️ Параметры полета:
- Дальность: {config['distance']:.2f} км
- Время: {config['flight_time']:.2f} ч
- Скорость: {config['speed']} км/ч
- Маневры: {config['maneuver_time']}% времени

🦾 Комплектация:
- АКБ:
{config['battery_info']}

- Электромотор:
{config['rotor_info']}"""
        
        keyboard = [
            [InlineKeyboardButton("⬅ Назад", callback_data="history")],
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
    
    # Удаляем все сообщения, включая приветственное
    await delete_messages(context, chat_id, keep_ids=[])  # Указываем пустой keep_ids
    
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
    
    summary = f"""📋 Подтвердите параметры:
- Тип БВС: {'Барражирующий' if context.user_data['type'] == 'loitering' else 'Дальний'}
- Время полета: {context.user_data['flight_time']:.2f} ч
- Дальность: {context.user_data['distance']:.2f} км
- Скорость: {context.user_data['speed']} км/ч
- Полезная нагрузка: {context.user_data['payload']} кг
- Аэродинамическое качество: {context.user_data['aero_quality']}
- Запас по тяге: {context.user_data['thrust_reserve']}
- Маневры: {context.user_data['maneuver_time']}% времени
- Материал планера: {context.user_data['plane_mass']}
- КПД винта: {context.user_data['propeller_eff']}
- Тип взлета: {context.user_data['takeoff_type']}
"""
    prompt_msg = await send_message(
        update, context,
        summary,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_calc")],
            [InlineKeyboardButton("🔄 Изменить", callback_data="restart")]
        ])
    )
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для подтверждения параметров")
    logger.debug(f"Текущее состояние message_ids после выбора типа взлета: {context.user_data['message_ids']}")
    logger.info(f"Пользователь {user_id} подтвердил параметры для расчета")
    return CALCULATE

async def input_config_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода имени конфигурации с очисткой сообщений после сохранения"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    config_name = update.message.text.strip()
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для ввода имени конфигурации")
    
    if not config_name:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Введите непустое название конфигурации:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке ввода имени")
        return INPUT_CONFIG_NAME

    configs = load_configs()
    if str(user_id) in configs and config_name in configs[str(user_id)]:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Конфигурация с таким названием уже существует. Введите другое название:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке имени")
        return INPUT_CONFIG_NAME

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = context.user_data
    config_data = {
        'config_name': config_name,
        'flight_time': data['flight_time'],
        'distance': data['distance'],
        'speed': data['speed'],
        'payload': data['payload'],
        'aero_quality': data['aero_quality'],
        'thrust_reserve': data['thrust_reserve'],
        'maneuver_time': data['maneuver_time'],
        'plane_mass': data['plane_mass'],
        'propeller_eff': data['propeller_eff'],
        'takeoff_type': data['takeoff_type'],
        'battery_capacity': data['battery_capacity'],
        'takeoff_mass': data['takeoff_mass'],
        'thrust_cruise': data['thrust_cruise'],
        'thrust_max': data['thrust_max'],
        'power_cruise': data['power_cruise'],
        'power_max': data['power_max'],
        'battery_mass': data['battery_mass'],
        'battery_voltage': data['battery_voltage'],
        'battery_capacity_ah': data['battery_capacity_ah'],
        'battery_capacity_recommended': data['battery_capacity_recommended'],
        'battery_type': data['battery_type'],
        'battery_info': data['battery_info'],
        'rotor_info': data['rotor_info'],
        'created_at': timestamp
    }

    if str(user_id) not in configs:
        configs[str(user_id)] = {}
    configs[str(user_id)][config_name] = config_data
    save_configs(configs)
    if os.getenv('RENDER'):
        update_repo()

    await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])
    prompt_msg = await send_message(
        update, context,
        f"✅ Конфигурация '{config_name}' сохранена!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🛠 Новый расчет", callback_data="restart")]
        ])
    )
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения о сохранении конфигурации")
    logger.info(f"Пользователь {user_id} сохранил конфигурацию: {config_name}")
    return CALCULATE

async def calculate_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    start_time = time.time()
    chat_id = update.effective_chat.id
    data = context.user_data
    user_id = update.effective_user.id
    file_path = Path('база.xlsx')
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для сообщения пользователя в calculate_results")
    
    try:
        # Проверка файла
        if not file_path.exists():
            await send_message(
                update, context,
                "⚠️ Ошибка: Файл 'база.xlsx' не найден.",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.error(f"Excel file {file_path} not found")
            return ConversationHandler.END
        
        xls = pd.ExcelFile(file_path)
        required_sheets = ['АКБ', 'Электророторы']
        if not all(s in xls.sheet_names for s in required_sheets):
            await send_message(
                update, context,
                "⚠️ Ошибка: Файл 'база.xlsx' не содержит нужные листы.",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.error("Invalid Excel file structure: missing sheets")
            return ConversationHandler.END
        
        df_akb = pd.read_excel(xls, sheet_name='АКБ')
        df_electrorotors = pd.read_excel(xls, sheet_name='Электророторы')
        
        required_columns_akb = ['Наименование', 'Ссылка', 'Напряжение (В)', 'Емкость (mah)', 'Вес (кг)']
        required_columns_rotors = ['Наименование', 'Ссылка', 'Мощность (кВт)', 'Напряжение (В)']
        if not all(col in df_akb.columns for col in required_columns_akb) or \
           not all(col in df_electrorotors.columns for col in required_columns_rotors):
            await send_message(
                update, context,
                "⚠️ Ошибка: Неправильная структура файла 'база.xlsx'.",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.error("Invalid Excel file structure: missing columns")
            return ConversationHandler.END

        # Проверка параметров
        required_keys = ['payload', 'distance', 'maneuver_time', 'battery_capacity', 'aero_quality', 'propeller_eff', 'thrust_reserve', 'plane_mass', 'speed', 'takeoff_type']
        if not all(key in data for key in required_keys):
            missing_keys = [key for key in required_keys if key not in data]
            await send_message(
                update, context,
                f"⚠️ Ошибка: Отсутствуют параметры: {', '.join(missing_keys)}",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.error(f"Missing parameters in context.user_data: {missing_keys}")
            return ConversationHandler.END

        G_pn = data['payload']
        L = data['distance']
        x = data['maneuver_time']
        E_a = data['battery_capacity']
        K = data['aero_quality']
        n = data['propeller_eff']
        E = data['thrust_reserve']
        G_plan = data['plane_mass']
        V = data['speed']
        T_W = data['takeoff_type']
        
        logger.info(f"Параметры: G_pn={G_pn}, L={L}, x={x}, E_a={E_a}, K={K}, n={n}, E={E}, G_plan={G_plan}, V={V}, T_W={T_W}")
        
        cruise_part = (100 * L * (100 - x)) / (100 * 27 * E_a * K * n)
        maneuver_part = (100 * L * E * x) / (2700 * E_a * K * n)
        denominator = 1 - cruise_part - maneuver_part - G_plan
        logger.info(f"cruise_part={cruise_part}, maneuver_part={maneuver_part}, denominator={denominator}")
        
        if denominator <= 0:
            error_message = f"⚠️ <b>Ошибка расчета:</b> Невозможная комбинация параметров (сумма долей массы: {denominator:.2f}).\n\n"
            error_message += "Сумма долей массы превышает допустимую величину.\nРекомендации:\n"
            recommendations = []
            if data['type'] == "loitering":
                error_message += f"- Рассчитанная дальность: {L:.2f} км\n"
                if L > 300:
                    recommendations.append(f"Уменьшите время полета (сейчас {data['flight_time']:.2f} ч)")
            else:
                if L > 300:
                    recommendations.append(f"Уменьшите дальность (сейчас {L:.2f} км)")
            if x > 15:
                recommendations.append(f"Снизьте % маневрирования (сейчас {x}%)")
            if K < 12:
                recommendations.append("Увеличьте аэродинамическое качество")
            if E > 1.5:
                recommendations.append("Уменьшите запас по тяге")
            
            error_message += "\n".join([f"• {r}" for r in recommendations]) if recommendations else "• Попробуйте изменить параметры"
            error_message += "\n\nВыберите параметр для изменения:"
            
            keyboard = [
                [InlineKeyboardButton("Время/дальность", callback_data="change_flight_time")],
                [InlineKeyboardButton("Скорость", callback_data="change_speed")],
                [InlineKeyboardButton("Аэродинамика", callback_data="change_aero_quality")],
                [InlineKeyboardButton("Маневры", callback_data="change_maneuver_time")],
                [InlineKeyboardButton("Начать заново", callback_data="restart")]
            ]
            sent_msg = await send_message(update, context, error_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            context.user_data['result_message_id'] = sent_msg.message_id
            context.user_data['current_config'] = data
            logger.info(f"Отрицательный знаменатель: {denominator}, отправлено сообщение с рекомендациями ID {sent_msg.message_id}")
            return CALCULATE
        
        G_vzl = G_pn / denominator
        P = G_vzl / K
        N = (E * P * V * 100) / (27 * n)
        N_max = (E * G_vzl * T_W * V * 100) / (27 * n)
        
        if G_vzl <= 0 or P <= 0 or N <= 0 or N_max <= 0:
            await send_message(
                update, context,
                "⚠️ Ошибка: Расчет дал нереалистичные значения. Пожалуйста, начните заново с другими параметрами.",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.error(f"Invalid calculation results: G_vzl={G_vzl}, P={P}, N={N}, N_max={N_max}")
            return ConversationHandler.END
        
        battery_share = cruise_part + maneuver_part
        m_akb = battery_share * G_vzl
        U_akb = 22.2 if G_vzl < 15 else 44.4 if 15 <= G_vzl <= 50 else 51.8
        C_akb = (m_akb * E_a) / U_akb
        C_akb_recommended = C_akb * 1.15
        battery_type = 'Li-ion' if E_a == 300 else 'LiPo'

        suitable_akb = df_akb[
            (abs(df_akb['Напряжение (В)'] - U_akb) <= 2) &
            (df_akb['Емкость (mah)']/1000 >= C_akb*1.1) &
            (df_akb['Вес (кг)'] <= m_akb)
        ].iloc[0:1]
        
        suitable_rotors = df_electrorotors[
            (df_electrorotors['Мощность (кВт)'] >= N/1000) &
            (df_electrorotors['Напряжение (В)'] < U_akb)
        ].iloc[0:1]
        
        akb = (f"{suitable_akb.iloc[0]['Наименование']} - {suitable_akb.iloc[0]['Ссылка']}" 
               if not suitable_akb.empty 
               else "⚠️ Подходящий АКБ не найден. Попробуйте уменьшить массу или увеличить напряжение.")
        rotor = (f"{suitable_rotors.iloc[0]['Наименование']} - {suitable_rotors.iloc[0]['Ссылка']}" 
                 if not suitable_rotors.empty 
                 else "⚠️ Подходящий электромотор не найден. Попробуйте изменить параметры мощности.")

        data['takeoff_mass'] = G_vzl
        data['thrust_cruise'] = P
        data['thrust_max'] = E * P
        data['power_cruise'] = N
        data['power_max'] = N_max
        data['battery_mass'] = m_akb
        data['battery_voltage'] = U_akb
        data['battery_capacity_ah'] = C_akb
        data['battery_capacity_recommended'] = C_akb_recommended
        data['battery_type'] = battery_type
        data['battery_info'] = akb
        data['rotor_info'] = rotor
        context.user_data['current_config'] = data

        result_text = f"""📊 Результаты расчета

🔹 Взлетная масса: {G_vzl:.2f} кг
🔹 Тяга: {P:.2f} кгс (крейсер), {E*P:.2f} кгс (макс)
🔹 Мощность: {N/1000:.2f} кВт (крейсер), {N_max/1000:.2f} кВт (макс)

🔋 Аккумулятор {battery_type}:
- Масса: {m_akb:.2f} кг
- Напряжение: {U_akb} В
- Емкость: {C_akb:.2f} А·ч (рекомендуется {C_akb_recommended:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {L:.2f} км
- Время: {data['flight_time']:.2f} ч
- Скорость: {V} км/ч
- Маневры: {x}% времени

🦾 Предложения по комплектации:
- АКБ:
{akb}

- Электромотор:
{rotor}"""
        
        logger.info(f"Message IDs before deletion in calculate_results: {context.user_data.get('message_ids', [])}")
        await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])
        logger.info(f"Message IDs after deletion: {context.user_data.get('message_ids', [])}")
        
        keyboard = [
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
        ]
        
        sent_msg = await send_message(update, context, result_text, reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data['result_message_id'] = sent_msg.message_id
        logger.info(f"Расчет завершен за {time.time() - start_time:.2f} секунд для пользователя {user_id}, отправлено сообщение с результатами ID {sent_msg.message_id}")
        logger.debug(f"Текущее состояние message_ids после отправки результата: {context.user_data['message_ids']}")
        return CALCULATE
        
    except Exception as e:
        logger.error(f"Ошибка расчета: {e}")
        await send_message(
            update, context,
            "⚠️ Ошибка расчета! Начните заново командой /start",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
        
async def handle_changes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка изменений параметров и расчета"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для обработки изменений")

    await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])

    if query.data == "back_to_welcome":
        welcome_text = """
🚀 *DroneDesigner* — Telegram-бот для расчёта параметров БПЛА

• Масса конструкции
• Требуемая мощность
• Параметры батареи
• И другие ключевые характеристики

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

    elif query.data == "history":
        configs = load_configs()
        user_configs = configs.get(str(user_id), {})
        keyboard = []
        if user_configs:
            keyboard = [
                [InlineKeyboardButton(f"{name} ({data['created_at']})", callback_data=f"config_{name}")]
                for name, data in user_configs.items()
            ]
        # Всегда добавляем кнопку "Вернуться к расчётам", если есть текущая конфигурация
        if context.user_data.get('current_config'):
            keyboard.append([InlineKeyboardButton("⬅ Вернуться к расчётам", callback_data="back_to_current")])
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])

        text = "📜 Выберите конфигурацию из списка:" if user_configs else \
               "⏳ У вас пока нет сохранённых конфигураций. Создайте свою первую конфигурацию!"
        await send_message(
            update, context,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.info(f"Пользователь {user_id} запросил историю конфигураций")
        return SHOW_HISTORY

    elif query.data == "restart":
        context.user_data.clear()
        context.user_data['message_ids'] = [context.user_data.get('welcome_message_id')] if context.user_data.get('welcome_message_id') else []
        keyboard = [
            [InlineKeyboardButton("Барражирующий БВС", callback_data="loitering")],
            [InlineKeyboardButton("БВС дальнего действия", callback_data="long_range")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите тип БВС:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для выбора типа БВС")
        logger.info(f"Пользователь {user_id} перезапустил конфигурацию")
        return CHOOSE_TYPE

    elif query.data == "save_config":
        prompt_msg = await send_message(
            update, context,
            "Введите название конфигурации:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса имени конфигурации")
        return INPUT_CONFIG_NAME

    elif query.data == "change_flight_time":
        prompt_msg = await send_message(
            update, context,
            "Введите новое значение времени полета в часах:" if context.user_data.get('type') == "loitering" 
            else "Введите новое значение дальности в км:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса нового времени/дальности")
        return CHANGE_FLIGHT_TIME

    elif query.data == "change_speed":
        prompt_msg = await send_message(
            update, context,
            "Введите новое значение скорости в км/ч:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса новой скорости")
        return CHANGE_SPEED

    elif query.data == "change_aero_quality":
        keyboard = [
            [InlineKeyboardButton("6 (Плохое)", callback_data="6")],
            [InlineKeyboardButton("8 (Среднее)", callback_data="8")],
            [InlineKeyboardButton("12 (Хорошее)", callback_data="12")],
            [InlineKeyboardButton("14 (Отличное)", callback_data="14")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите новое аэродинамическое качество:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса аэродинамического качества")
        return CHANGE_AERO_QUALITY

    elif query.data == "change_maneuver_time":
        keyboard = [
            [InlineKeyboardButton("10%", callback_data="10")],
            [InlineKeyboardButton("15%", callback_data="15")],
            [InlineKeyboardButton("30%", callback_data="30")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите новое время маневрирования:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса времени маневрирования")
        return CHANGE_MANEUVER_TIME

    elif query.data == "confirm_calc":
        prompt_msg = await send_message(update, context, "⏳ Выполняю расчет...", reply_markup=ReplyKeyboardRemove())
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения о начале расчета")
        return await calculate_results(update, context)

    elif query.data == "change_params":
        keyboard = [
            [InlineKeyboardButton("Время/дальность", callback_data="change_flight_time")],
            [InlineKeyboardButton("Скорость", callback_data="change_speed")],
            [InlineKeyboardButton("Аэродинамика", callback_data="change_aero_quality")],
            [InlineKeyboardButton("Маневры", callback_data="change_maneuver_time")],
            [InlineKeyboardButton("Начать заново", callback_data="restart")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите параметр для изменения:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для выбора параметра для изменения")
        return CALCULATE

    # Если ни одно из условий не выполнено, возвращаем результаты расчета
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
    logger.debug(f"Добавлен message_id {prompt_msg.message_id} для отображения результатов расчета")
    return CALCULATE

    keyboard = [
        [InlineKeyboardButton(f"{name} ({created_at})", callback_data=f"config_{id}")]
        for id, name, created_at in configs
    ]
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        
    await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])
    await send_message(
        update, context,
        "📜 Ваши сохраненные конфигурации:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SHOW_HISTORY
    
    if query.data == "restart":
        context.user_data['message_ids'] = [context.user_data.get('welcome_message_id')] if context.user_data.get('welcome_message_id') else []
        keyboard = [
            [InlineKeyboardButton("Барражирующий БВС", callback_data="loitering")],
            [InlineKeyboardButton("БВС дальнего действия", callback_data="long_range")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите тип БВС:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для выбора типа БВС")
        logger.info(f"Пользователь {user_id} перезапустил конфигурацию")
        return CHOOSE_TYPE
    elif query.data == "change_flight_time":
        prompt_msg = await send_message(
            update, context,
            "Введите новое значение времени полета в часах:" if context.user_data['type'] == "loitering" 
            else "Введите новое значение дальности в км:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса нового времени/дальности")
        return CHANGE_FLIGHT_TIME
    elif query.data == "change_speed":
        prompt_msg = await send_message(
            update, context,
            "Введите новое значение скорости в км/ч:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса новой скорости")
        return CHANGE_SPEED
    elif query.data == "change_aero_quality":
        keyboard = [
            [InlineKeyboardButton("6 (Плохое)", callback_data="6")],
            [InlineKeyboardButton("8 (Среднее)", callback_data="8")],
            [InlineKeyboardButton("12 (Хорошее)", callback_data="12")],
            [InlineKeyboardButton("14 (Отличное)", callback_data="14")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите новое аэродинамическое качество:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса аэродинамического качества")
        return CHANGE_AERO_QUALITY
    elif query.data == "change_maneuver_time":
        keyboard = [
            [InlineKeyboardButton("10%", callback_data="10")],
            [InlineKeyboardButton("15%", callback_data="15")],
            [InlineKeyboardButton("30%", callback_data="30")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите новое время маневрирования:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса времени маневрирования")
        return CHANGE_MANEUVER_TIME
    elif query.data == "save_config":
        prompt_msg = await send_message(
            update, context,
            "Введите название конфигурации:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для запроса имени конфигурации")
        return INPUT_CONFIG_NAME
    elif query.data == "back_to_welcome":
        welcome_text = """
🚀 *DroneDesigner* — Telegram-бот для расчёта параметров БПЛА

• Масса конструкции
• Требуемая мощность
• Параметры батареи
• И другие ключевые характеристики

Для инженеров и энтузиастов БПЛА!
        """
        prompt_msg = await send_message(
            update, context,
            welcome_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 История", callback_data="history")],
                [InlineKeyboardButton("🛠 Создать конфигурацию", callback_data="new_config")]
            ]),
            parse_mode="Markdown"
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для возврата в главное меню")
        return WELCOME_STATE
    elif query.data == "confirm_calc":
        prompt_msg = await send_message(update, context, "⏳ Выполняю расчет...", reply_markup=ReplyKeyboardRemove())
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения о начале расчета")
        return await calculate_results(update, context)
    elif query.data == "change_params":
        keyboard = [
            [InlineKeyboardButton("Время/дальность", callback_data="change_flight_time")],
            [InlineKeyboardButton("Скорость", callback_data="change_speed")],
            [InlineKeyboardButton("Аэродинамика", callback_data="change_aero_quality")],
            [InlineKeyboardButton("Маневры", callback_data="change_maneuver_time")],
            [InlineKeyboardButton("Начать заново", callback_data="restart")]
        ]
        prompt_msg = await send_message(
            update, context,
            "Выберите параметр для изменения:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для выбора параметра для изменения")
        return CALCULATE

async def change_flight_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка нового времени полета или дальности"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if update.message and update.message.message_id:
        if update.message.message_id not in context.user_data['message_ids']:
            context.user_data['message_ids'].append(update.message.message_id)
            logger.debug(f"Добавлен message_id {update.message.message_id} для ввода нового времени/дальности")
    
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
            
        prompt_msg = await send_message(update, context, "⏳ Выполняю пересчет...", reply_markup=ReplyKeyboardRemove())
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения о пересчете")
        logger.info(f"Пользователь {user_id} изменил время/дальность на {value}")
        return await calculate_results(update, context)
        
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
    """Обработка новой скорости"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if update.message and update.message.message_id:
        context.user_data['message_ids'].append(update.message.message_id)
    
    try:
        speed = float(update.message.text)
        if speed <= 0:
            raise ValueError
            
        context.user_data['speed'] = speed
        
        if context.user_data['type'] == "loitering":
            context.user_data['distance'] = context.user_data['flight_time'] * speed
        else:
            context.user_data['flight_time'] = context.user_data['distance'] / speed
            
        await send_message(update, context, "⏳ Выполняю пересчет...", reply_markup=ReplyKeyboardRemove())
        logger.info(f"User {user_id} changed speed to {speed}")
        return await calculate_results(update, context)
        
    except ValueError:
        await send_message(
            update, context,
            "Ошибка! Введите положительное число:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]
            ])
        )
        return CHANGE_SPEED

async def change_aero_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка нового аэродинамического качества"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if query.message.message_id:
        context.user_data['message_ids'].append(query.message.message_id)
    
    if query.data not in SELECTION_MAPS['aero_quality']:
        await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("6 (Плохое)", callback_data="6")],
                [InlineKeyboardButton("8 (Среднее)", callback_data="8")],
                [InlineKeyboardButton("12 (Хорошее)", callback_data="12")],
                [InlineKeyboardButton("14 (Отличное)", callback_data="14")]
            ])
        )
        return CHANGE_AERO_QUALITY
    
    context.user_data['aero_quality'] = int(query.data)
    
    await send_message(update, context, "⏳ Выполняю пересчет...", reply_markup=ReplyKeyboardRemove())
    logger.info(f"User {user_id} changed aero quality to {query.data}")
    return await calculate_results(update, context)

async def change_maneuver_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка нового времени маневрирования"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if query.message.message_id:
        context.user_data['message_ids'].append(query.message.message_id)
    
    if query.data not in ["10", "15", "30"]:
        await send_message(
            update, context,
            "Ошибка! Неверный выбор. Попробуйте снова:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("10%", callback_data="10")],
                [InlineKeyboardButton("15%", callback_data="15")],
                [InlineKeyboardButton("30%", callback_data="30")]
            ])
        )
        return CHANGE_MANEUVER_TIME
    
    context.user_data['maneuver_time'] = float(query.data)
    
    await send_message(update, context, "⏳ Выполняю пересчет...", reply_markup=ReplyKeyboardRemove())
    logger.info(f"User {user_id} changed maneuver time to {query.data}%")
    return await calculate_results(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /cancel"""
    chat_id = update.effective_chat.id
    context.user_data.clear()
    context.user_data['message_ids'] = []
    await delete_messages(context, chat_id)
    
    await send_message(
        update, context,
        "Диалог прерван. Для нового расчета введите /start",
        reply_markup=ReplyKeyboardRemove()
    )
    logger.info(f"Пользователь {update.effective_user.id} отменил диалог")
    return ConversationHandler.END

def main() -> None:
    """Запуск бота"""
    logger.info("Запуск бота...")
    logger.info(f"Токен: {'Загружен' if TOKEN else 'Не найден'}")
    
    if not TOKEN:
        logger.error("Токен бота не найден в переменных окружения")
        raise ValueError("BOT_TOKEN не установлен")
    
    if os.getenv('RENDER') and GIT_TOKEN:
        subprocess.run(['git', 'remote', 'set-url', 'origin', f'https://{GIT_TOKEN}@github.com/crystalglock1/DroneDesigner.git'])
    
    try:
        # Создаем Application
        application = Application.builder().token(TOKEN).build()
        logger.info("Application успешно создан")
        
        # Создаем ConversationHandler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                WELCOME_STATE: [CallbackQueryHandler(handle_welcome)],
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
                CALCULATE: [CallbackQueryHandler(handle_changes)],
                CHANGE_FLIGHT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_flight_time)],
                CHANGE_SPEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_speed)],
                CHANGE_AERO_QUALITY: [CallbackQueryHandler(change_aero_quality)],
                CHANGE_MANEUVER_TIME: [CallbackQueryHandler(change_maneuver_time)],
                SHOW_HISTORY: [CallbackQueryHandler(show_history)],
                SHOW_CONFIG: [CallbackQueryHandler(show_config)],
                CONFIRM_DELETE: [CallbackQueryHandler(confirm_delete)],
                INPUT_CONFIG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_config_name)]
            },
            fallbacks=[
                CommandHandler('cancel', cancel),
                CommandHandler('start', start)
            ],
            per_message=False
        )
        
        # Добавляем обработчик
        application.add_handler(conv_handler)
        
        # Запускаем polling
        logger.info("Запуск polling...")
        application.run_polling(allowed_updates=["message", "callback_query"])
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        raise

if __name__ == '__main__':
    main()
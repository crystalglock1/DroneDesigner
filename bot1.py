import logging
import sqlite3
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

# Токен бота
TOKEN = "7307737118:AAH963acFly_MjqnXkE2a8OhrfZjlYm1o50"

# Словарь для маппинга выбора
SELECTION_MAPS = {
    'aero_quality': {"6": 6, "8": 8, "12": 12, "14": 14},
    'thrust_reserve': {"1.5": 1.5, "2.0": 2.0, "3.0": 3.0},
    'plane_material': {"0.40": 0.40, "0.45": 0.45, "0.50": 0.50},
    'propeller_eff': {"0.75": 0.75, "0.80": 0.80},
    'takeoff_type': {"0.3": 0.3, "0.4": 0.4, "0.6": 0.6}
}

# Инициализация базы данных SQLite
def init_db():
    """Инициализация базы данных для хранения конфигураций"""
    conn = sqlite3.connect('configurations.db')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(configurations)")
    columns = [info[1] for info in cursor.fetchall()]
    expected_columns = [
        'id', 'user_id', 'config_name', 'unique_key', 'flight_time', 'distance', 'speed', 'payload',
        'aero_quality', 'thrust_reserve', 'maneuver_time', 'plane_mass', 'propeller_eff',
        'takeoff_type', 'battery_capacity', 'takeoff_mass', 'thrust_cruise', 'thrust_max',
        'power_cruise', 'power_max', 'battery_mass', 'battery_voltage', 'battery_capacity_ah',
        'battery_capacity_recommended', 'battery_type', 'battery_info', 'rotor_info', 'created_at'
    ]
    if not all(col in columns for col in expected_columns):
        cursor.execute("DROP TABLE IF EXISTS configurations")
        cursor.execute('''
            CREATE TABLE configurations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                config_name TEXT,
                unique_key TEXT UNIQUE,
                flight_time REAL,
                distance REAL,
                speed REAL,
                payload REAL,
                aero_quality INTEGER,
                thrust_reserve REAL,
                maneuver_time REAL,
                plane_mass REAL,
                propeller_eff REAL,
                takeoff_type REAL,
                battery_capacity REAL,
                takeoff_mass REAL,
                thrust_cruise REAL,
                thrust_max REAL,
                power_cruise REAL,
                power_max REAL,
                battery_mass REAL,
                battery_voltage REAL,
                battery_capacity_ah REAL,
                battery_capacity_recommended REAL,
                battery_type TEXT,
                battery_info TEXT,
                rotor_info TEXT,
                created_at TEXT,
                UNIQUE(user_id, config_name)
            )
        ''')
    conn.commit()
    conn.close()

# Очистка устаревших конфигураций
def cleanup_old_configs(user_id, max_age_days=30, max_configs=50):
    """Удаление конфигураций старше max_age_days или сверх max_configs"""
    conn = sqlite3.connect('configurations.db')
    cursor = conn.cursor()
    cursor.execute('''
        DELETE FROM configurations 
        WHERE user_id = ? AND created_at < datetime('now', ?)
    ''', (user_id, f'-{max_age_days} days'))
    cursor.execute('''
        DELETE FROM configurations 
        WHERE user_id = ? AND id NOT IN (
            SELECT id FROM configurations WHERE user_id = ? 
            ORDER BY created_at DESC LIMIT ?
        )
    ''', (user_id, user_id, max_configs))
    conn.commit()
    conn.close()

# Инициализация базы данных
init_db()

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
    
    context.user_data['last_start_time'] = datetime.now()  # Исправлено здесь
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

    elif query.data == "new_config":
        keyboard = [
            [InlineKeyboardButton("Барражирующий БВС", callback_data="loitering")],
            [InlineKeyboardButton("БВС дальнего действия", callback_data="long_range")]
        ]
        sent_msg = await context.bot.send_message(  # Отправляем новое сообщение
            chat_id=chat_id,
            text="Выберите тип БВС:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data['message_ids'].append(sent_msg.message_id)
        logger.info(f"Пользователь {user_id} выбрал новую конфигурацию, отправлено сообщение {sent_msg.message_id}")
        return CHOOSE_TYPE
        
    elif query.data == "history":
        conn = sqlite3.connect('configurations.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, config_name, created_at FROM configurations WHERE user_id = ?', (user_id,))
        configs = cursor.fetchall()
        conn.close()
        if not configs:
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
            [InlineKeyboardButton(f"{name} ({created_at})", callback_data=f"config_{id}")]
            for id, name, created_at in configs
        ]
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        await send_message(update, context, "📜 Выберите конфигурацию из списка:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SHOW_HISTORY
    
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
        return await handle_changes(update, context)

    if match := re.match(r"config_(\d+)", query.data):
        config_id = int(match.group(1))
        conn = sqlite3.connect('configurations.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM configurations WHERE id = ? AND user_id = ?', (config_id, user_id))
        config = cursor.fetchone()
        conn.close()

        if not config:
            await send_message(
                update, context,
                "⚠️ Конфигурация не найдена. Вернитесь в главное меню.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Назад", callback_data="history")]
                ])
            )
            return SHOW_HISTORY

        result_text = f"""📊 Конфигурация: {config[2]} ({config[27]})

🔹 Взлетная масса: {config[15]:.2f} кг
🔹 Тяга: {config[16]:.2f} кгс (крейсер), {config[17]:.2f} кгс (макс)
🔹 Мощность: {config[18]/1000:.2f} кВт (крейсер), {config[19]/1000:.2f} кВт (макс)

🔋 Аккумулятор {config[24]}:
- Масса: {config[20]:.2f} кг
- Напряжение: {config[21]} В
- Емкость: {config[22]:.2f} А·ч (рекомендуется {config[23]:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {config[5]:.2f} км
- Время: {config[4]:.2f} ч
- Скорость: {config[6]} км/ч
- Маневры: {config[10]}% времени

🦾 Комплектация:
- АКБ:
{config[25]}

- Электромотор:
{config[26]}"""
        
        keyboard = [
            [InlineKeyboardButton("⬅ Назад к списку", callback_data="history")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{config_id}")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} просмотрел конфигурацию {config_id}")
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
        conn = sqlite3.connect('configurations.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, config_name, created_at FROM configurations WHERE user_id = ?', (user_id,))
        configs = cursor.fetchall()
        conn.close()

        if not configs:
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
            [InlineKeyboardButton(f"{name} ({created_at})", callback_data=f"config_{id}")]
            for id, name, created_at in configs
        ]
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        await send_message(update, context, "📜 Выберите конфигурацию из списка:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SHOW_HISTORY

    if match := re.match(r"delete_(\d+)", query.data):
        config_id = int(match.group(1))
        await send_message(
            update, context,
            "Вы точно хотите удалить конфигурацию?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Удалить", callback_data=f"confirm_delete_{config_id}")],
                [InlineKeyboardButton("🚫 Отмена", callback_data=f"config_{config_id}")]
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

    if match := re.match(r"confirm_delete_(\d+)", query.data):
        config_id = int(match.group(1))
        conn = sqlite3.connect('configurations.db')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM configurations WHERE id = ? AND user_id = ?', (config_id, user_id))
        conn.commit()
        conn.close()
        logger.info(f"Пользователь {user_id} удалил конфигурацию {config_id}")
        
        conn = sqlite3.connect('configurations.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, config_name, created_at FROM configurations WHERE user_id = ?', (user_id,))
        configs = cursor.fetchall()
        conn.close()

        if not configs:
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
            [InlineKeyboardButton(f"{name} ({created_at})", callback_data=f"config_{id}")]
            for id, name, created_at in configs
        ]
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        await send_message(update, context, "📜 Конфигурация удалена. Выберите другую конфигурацию:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SHOW_HISTORY

    if match := re.match(r"config_(\d+)", query.data):
        config_id = int(match.group(1))
        conn = sqlite3.connect('configurations.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM configurations WHERE id = ? AND user_id = ?', (config_id, user_id))
        config = cursor.fetchone()
        conn.close()

        if not config:
            await send_message(
                update, context,
                "⚠️ Конфигурация не найдена. Вернитесь в главное меню.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Назад", callback_data="history")]
                ])
            )
            return SHOW_HISTORY

        result_text = f"""📊 Конфигурация: {config[2]} ({config[27]})

🔹 Взлетная масса: {config[15]:.2f} кг
🔹 Тяга: {config[16]:.2f} кгс (крейсер), {config[17]:.2f} кгс (макс)
🔹 Мощность: {config[18]/1000:.2f} кВт (крейсер), {config[19]/1000:.2f} кВт (макс)

🔋 Аккумулятор {config[24]}:
- Масса: {config[20]:.2f} кг
- Напряжение: {config[21]} В
- Емкость: {config[22]:.2f} А·ч (рекомендуется {config[23]:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {config[5]:.2f} км
- Время: {config[4]:.2f} ч
- Скорость: {config[6]} км/ч
- Маневры: {config[10]}% времени

🦾 Комплектация:
- АКБ:
{config[25]}

- Электромотор:
{config[26]}"""
        
        keyboard = [
            [InlineKeyboardButton("⬅ Назад", callback_data="history")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{config_id}")]
        ]
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {user_id} просмотрел конфигурацию {config_id}")
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

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    unique_key = f"{user_id}:{config_name}:{timestamp}"

    data = context.user_data
    conn = sqlite3.connect('configurations.db')
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO configurations (
                user_id, config_name, unique_key, flight_time, distance, speed, payload,
                aero_quality, thrust_reserve, maneuver_time, plane_mass, propeller_eff,
                takeoff_type, battery_capacity, takeoff_mass, thrust_cruise, thrust_max,
                power_cruise, power_max, battery_mass, battery_voltage, battery_capacity_ah,
                battery_capacity_recommended, battery_type, battery_info, rotor_info, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, config_name, unique_key, data['flight_time'], data['distance'], data['speed'],
            data['payload'], data['aero_quality'], data['thrust_reserve'], data['maneuver_time'],
            data['plane_mass'], data['propeller_eff'], data['takeoff_type'], data['battery_capacity'],
            data['takeoff_mass'], data['thrust_cruise'], data['thrust_max'], data['power_cruise'],
            data['power_max'], data['battery_mass'], data['battery_voltage'], data['battery_capacity_ah'],
            data['battery_capacity_recommended'], data['battery_type'], data['battery_info'],
            data['rotor_info'], timestamp
        ))
        conn.commit()
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
    except sqlite3.IntegrityError:
        prompt_msg = await send_message(
            update, context,
            "Ошибка! Конфигурация с таким названием уже существует. Введите другое название:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.debug(f"Добавлен message_id {prompt_msg.message_id} для сообщения об ошибке имени")
        return INPUT_CONFIG_NAME
    finally:
        conn.close()
    
    cleanup_old_configs(user_id)
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
    """Обработка изменений параметров"""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if 'message_ids' not in context.user_data:
        context.user_data['message_ids'] = []
    
    if query.message.message_id and query.message.message_id not in context.user_data['message_ids']:
        context.user_data['message_ids'].append(query.message.message_id)
        logger.debug(f"Добавлен message_id {query.message.message_id} для обработки изменений параметров")
    
    if query.data == "history":
        conn = sqlite3.connect('configurations.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, config_name, created_at FROM configurations WHERE user_id = ?', (user_id,))
        configs = cursor.fetchall()
        conn.close()
        
        keyboard = [
            [InlineKeyboardButton(f"{name} ({created_at})", callback_data=f"config_{id}")]
            for id, name, created_at in configs
        ]
        keyboard.append([InlineKeyboardButton("⬅ Назад к расчету", callback_data="back_to_current")])
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")])
        
        await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])
        await send_message(
            update, context,
            "📜 Ваши сохраненные конфигурации:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SHOW_HISTORY
    
    elif query.data == "back_to_current":
        # Восстанавливаем текущую конфигурацию из user_data
        data = context.user_data.get('current_config', {})
        if not data:
            return await handle_welcome(update, context)
            
        result_text = f"""📊 Результаты расчета

🔹 Взлетная масса: {data['takeoff_mass']:.2f} кг
🔹 Тяга: {data['thrust_cruise']:.2f} кгс (крейсер), {data['thrust_max']:.2f} кгс (макс)
🔹 Мощность: {data['power_cruise']/1000:.2f} кВт (крейсер), {data['power_max']/1000:.2f} кВт (макс)

🔋 Аккумулятор {data['battery_type']}:
- Масса: {data['battery_mass']:.2f} кг
- Напряжение: {data['battery_voltage']} В
- Емкость: {data['battery_capacity_ah']:.2f} А·ч (рекомендуется {data['battery_capacity_recommended']:.2f} А·ч)

✈️ Параметры полета:
- Дальность: {data['distance']:.2f} км
- Время: {data['flight_time']:.2f} ч
- Скорость: {data['speed']} км/ч
- Маневры: {data['maneuver_time']}% времени

🦾 Предложения по комплектации:
- АКБ:
{data['battery_info']}

- Электромотор:
{data['rotor_info']}"""
        
        keyboard = [
            [InlineKeyboardButton("📖 История", callback_data="history")],
            [InlineKeyboardButton("💾 Сохранить конфигурацию", callback_data="save_config")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_welcome")],
            [InlineKeyboardButton("🔄 Изменить параметры", callback_data="change_params")]
        ]
        
        await delete_messages(context, chat_id, keep_ids=[context.user_data.get('welcome_message_id')])
        await send_message(
            update, context,
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
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
    logger.info(f"User {update.effective_user.id} cancelled the conversation")
    return ConversationHandler.END

def main() -> None:
    """Запуск бота"""
    application = Application.builder().token(TOKEN).build()
    
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
    
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import aiosqlite

# ---------- НАСТРОЙКИ ----------
TOKEN = "8785273956:AAF8mdNuhjeM2Onrbqs0xeG3fYG-arQNI9k" # Токен бота
ARTEM_ID = 1172985519             # ID Telegram Артема
BAZA_ID = 987654321                # ID Telegram Базы

# ID администраторов (имеют доступ ко всем функциям)
ADMIN_IDS = {ARTEM_ID, BAZA_ID}

# ---------- БАЗА ДАННЫХ ----------
DB_NAME = "farm.db"

async def init_db():
    """Создаёт таблицы, если их нет."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица принтеров (реестр)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS printers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'работает',  -- работает, сломан, снят
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Журнал поломок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS breakdowns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_id INTEGER NOT NULL,
                reason TEXT,
                reported_by INTEGER,          -- Telegram ID
                reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                resolved_by INTEGER,
                FOREIGN KEY(printer_id) REFERENCES printers(id)
            )
        """)
        # Склад деталей (текущее количество)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT,
                quantity_packed INTEGER DEFAULT 0
            )
        """)
        # Лог упаковок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS packing_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id INTEGER,
                user_id INTEGER,
                qty_added INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(part_id) REFERENCES parts(id)
            )
        """)
        # Лог отгрузок (обнуление количества)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id INTEGER,
                user_id INTEGER,
                qty_shipped INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(part_id) REFERENCES parts(id)
            )
        """)
        await db.commit()

# ---------- СОСТОЯНИЯ FSM ----------
class PrinterBreak(StatesGroup):
    waiting_for_number = State()
    waiting_for_reason = State()

class PrinterReturn(StatesGroup):
    waiting_for_selection = State()

class PartAdd(StatesGroup):
    waiting_for_code = State()
    waiting_for_name = State()
    waiting_for_qty = State()

class AdminAddPrinter(StatesGroup):
    waiting_for_number = State()

class AdminRemovePrinter(StatesGroup):
    waiting_for_number = State()

class AdminShipPart(StatesGroup):
    waiting_for_code = State()
    waiting_for_qty = State()  # для частичной отгрузки

class AdminStatHistory(StatesGroup):
    waiting_for_printer_number = State()

# ---------- КЛАВИАТУРЫ ----------
def main_menu(user_id: int):
    """Главное меню в зависимости от роли."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🖨️ Сломался принтер", callback_data="menu_break")
    kb.button(text="✅ Вернуть принтер в работу", callback_data="menu_return")
    kb.button(text="📋 Список неработающих", callback_data="menu_list_broken")
    kb.button(text="📦 Добавить упакованные детали", callback_data="menu_add_part")
    if user_id in ADMIN_IDS:
        kb.button(text="📊 Статистика принтеров", callback_data="menu_stats")
        kb.button(text="➕ Добавить принтер в систему", callback_data="menu_add_printer")
        kb.button(text="🗑️ Снять принтер с производства", callback_data="menu_remove_printer")
        kb.button(text="📋📦 Просмотр базы деталей", callback_data="menu_parts_list")
        kb.button(text="🚚 Очистить отгруженные", callback_data="menu_ship")
    kb.adjust(2)  # по 2 кнопки в ряд
    return kb.as_markup()

# ---------- ПОМОЩНИКИ ----------
async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def add_printer_to_db(number: str) -> bool:
    """Добавляет принтер в реестр, если его ещё нет. Возвращает True, если добавлен."""
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO printers (number, status) VALUES (?, 'работает')", (number,))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # уже существует

async def get_printer_id_by_number(number: str) -> int | None:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id FROM printers WHERE number = ? AND status != 'снят'", (number,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def set_printer_status(number: str, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE printers SET status = ? WHERE number = ?", (status, number))
        await db.commit()

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
async def start_command(message: Message):
    await message.answer("👋 Добро пожаловать в систему управления фермой 3D-печати!",
                         reply_markup=main_menu(message.from_user.id))

# ---------- СЛОМАЛСЯ ПРИНТЕР ----------
async def menu_break(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите номер принтера, который сломался:")
    await state.set_state(PrinterBreak.waiting_for_number)
    await callback.answer()

async def break_number(message: Message, state: FSMContext):
    number = message.text.strip()
    printer_id = await get_printer_id_by_number(number)
    if not printer_id:
        # возможно, принтер не в реестре – разрешим добавлять только админам?
        # По логике, оператор не может ломать не зарегистрированный принтер.
        # Но чтобы не усложнять, добавим проверку: если номер не найден, сообщим об этом.
        await message.answer("❌ Принтер с таким номером не найден в системе. Обратитесь к администратору.")
        await state.clear()
        return
    await state.update_data(printer_id=printer_id, number=number)
    await message.answer("Опишите причину поломки (например: брак, ось X, засор):")
    await state.set_state(PrinterBreak.waiting_for_reason)

async def break_reason(message: Message, state: FSMContext):
    reason = message.text.strip()
    data = await state.get_data()
    printer_id = data["printer_id"]
    number = data["number"]
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        # Создаём запись о поломке
        await db.execute(
            "INSERT INTO breakdowns (printer_id, reason, reported_by) VALUES (?, ?, ?)",
            (printer_id, reason, user_id)
        )
        # Меняем статус принтера на 'сломан'
        await db.execute("UPDATE printers SET status = 'сломан' WHERE id = ?", (printer_id,))
        await db.commit()

    await message.answer(f"✅ Принтер #{number} отмечен как сломаный. Причина: {reason}",
                         reply_markup=main_menu(user_id))
    await state.clear()

# ---------- ВЕРНУТЬ В РАБОТУ ----------
async def menu_return(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        # Получаем список сломаных принтеров с незакрытыми поломками
        cursor = await db.execute("""
            SELECT p.number, b.id, b.reason, b.reported_at
            FROM printers p
            JOIN breakdowns b ON p.id = b.printer_id
            WHERE p.status = 'сломан' AND b.resolved_at IS NULL
            ORDER BY b.reported_at DESC
        """)
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.edit_text("✅ Все принтеры работают! Нет неисправных.")
        await callback.answer()
        return

    kb = InlineKeyboardBuilder()
    for number, breakdown_id, reason, reported_at in rows:
        # Форматируем кнопку
        date_str = datetime.strptime(reported_at, "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M") if reported_at else "?"
        btn_text = f"#{number} — {reason} ({date_str})"
        kb.button(text=btn_text, callback_data=f"return_{breakdown_id}")

    kb.adjust(1)
    await callback.message.edit_text("Выберите принтер, который починили:", reply_markup=kb.as_markup())
    await callback.answer()

async def return_printer(callback: CallbackQuery):
    breakdown_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        # Получаем printer_id для этой поломки
        cursor = await db.execute("SELECT printer_id FROM breakdowns WHERE id = ?", (breakdown_id,))
        row = await cursor.fetchone()
        if not row:
            await callback.answer("Ошибка: запись не найдена.", show_alert=True)
            return
        printer_id = row[0]

        # Закрываем поломку
        await db.execute("UPDATE breakdowns SET resolved_at = CURRENT_TIMESTAMP, resolved_by = ? WHERE id = ?",
                         (user_id, breakdown_id))
        # Обновляем статус принтера
        await db.execute("UPDATE printers SET status = 'работает' WHERE id = ?", (printer_id,))
        # Получаем номер принтера для ответа
        cursor = await db.execute("SELECT number FROM printers WHERE id = ?", (printer_id,))
        number = (await cursor.fetchone())[0]
        await db.commit()

    await callback.message.edit_text(f"✅ Принтер #{number} снова в строю! Спасибо за починку.")
    await callback.answer("Принтер возвращён в работу.")

# ---------- СПИСОК НЕРАБОТАЮЩИХ ----------
async def menu_list_broken(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT p.number, b.reason, b.reported_at
            FROM printers p
            JOIN breakdowns b ON p.id = b.printer_id
            WHERE p.status = 'сломан' AND b.resolved_at IS NULL
            ORDER BY b.reported_at DESC
        """)
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.edit_text("✅ Все принтеры работают.")
        await callback.answer()
        return

    text = "🟥 **Неработающие принтеры:**\n"
    for number, reason, reported_at in rows:
        date_str = datetime.strptime(reported_at, "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"• #{number} — {reason} (с {date_str})\n"

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

# ---------- ДОБАВИТЬ УПАКОВАННЫЕ ДЕТАЛИ ----------
async def menu_add_part(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите код детали (артикул):")
    await state.set_state(PartAdd.waiting_for_code)
    await callback.answer()

async def part_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    await state.update_data(code=code)
    await message.answer("Введите название детали:")
    await state.set_state(PartAdd.waiting_for_name)

async def part_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(name=name)
    await message.answer("Введите количество упакованных штук:")
    await state.set_state(PartAdd.waiting_for_qty)

async def part_qty(message: Message, state: FSMContext):
    try:
        qty = int(message.text)
        if qty <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое положительное число.")
        return

    data = await state.get_data()
    code = data["code"]
    name = data["name"]
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        # Ищем деталь по коду
        cursor = await db.execute("SELECT id, quantity_packed FROM parts WHERE code = ?", (code,))
        row = await cursor.fetchone()
        if row:
            part_id, current_qty = row
            new_qty = current_qty + qty
            await db.execute("UPDATE parts SET quantity_packed = ? WHERE id = ?", (new_qty, part_id))
        else:
            # Создаём новую деталь
            cursor = await db.execute("INSERT INTO parts (code, name, quantity_packed) VALUES (?, ?, ?)",
                                      (code, name, qty))
            part_id = cursor.lastrowid
        # Логируем упаковку
        await db.execute("INSERT INTO packing_log (part_id, user_id, qty_added) VALUES (?, ?, ?)",
                         (part_id, user_id, qty))
        await db.commit()

    await message.answer(f"✅ Добавлено: {code} - {name}, +{qty} шт. На складе: {new_qty if row else qty}.",
                         reply_markup=main_menu(user_id))
    await state.clear()

# ---------- АДМИНКА: СТАТИСТИКА ПРИНТЕРОВ ----------
async def menu_stats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Количество сломанных сейчас
        cursor = await db.execute("SELECT COUNT(*) FROM printers WHERE status = 'сломан'")
        broken_now = (await cursor.fetchone())[0]

        # 2. Топ-5 проблемных принтеров (по количеству поломок за всё время)
        cursor = await db.execute("""
            SELECT p.number, COUNT(b.id) as cnt
            FROM breakdowns b
            JOIN printers p ON b.printer_id = p.id
            GROUP BY p.id
            ORDER BY cnt DESC
            LIMIT 5
        """)
        top = await cursor.fetchall()

    text = f"📊 Сейчас сломано: {broken_now}\n\n"
    if top:
        text += "🔝 Топ-5 проблемных принтеров:\n"
        for i, (num, cnt) in enumerate(top, 1):
            text += f"{i}. #{num} — {cnt} поломок\n"
    else:
        text += "Нет данных о поломках."

    kb = InlineKeyboardBuilder()
    kb.button(text="📜 История по номеру", callback_data="stat_history")
    kb.button(text="🔙 Назад", callback_data="back_to_main")
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()

async def stat_history_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите номер принтера для просмотра истории поломок:")
    await state.set_state(AdminStatHistory.waiting_for_printer_number)
    await callback.answer()

async def stat_history_show(message: Message, state: FSMContext):
    number = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("""
            SELECT b.reason, b.reported_at, b.resolved_at
            FROM breakdowns b
            JOIN printers p ON b.printer_id = p.id
            WHERE p.number = ? AND p.status != 'снят'
            ORDER BY b.reported_at DESC
        """, (number,))
        rows = await cursor.fetchall()
    if not rows:
        await message.answer(f"❌ Принтер #{number} не найден или по нему нет поломок.",
                             reply_markup=main_menu(message.from_user.id))
    else:
        text = f"📜 История поломок принтера #{number}:\n\n"
        for reason, reported, resolved in rows:
            rep_date = reported[:16] if reported else "?"
            res_date = resolved[:16] if resolved else "не починен"
            text += f"• {rep_date} — {reason} (починен: {res_date})\n"
        await message.answer(text, reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ---------- АДМИНКА: ДОБАВИТЬ ПРИНТЕР ----------
async def menu_add_printer(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("➕ Введите номер нового принтера:")
    await state.set_state(AdminAddPrinter.waiting_for_number)
    await callback.answer()

async def add_printer_number(message: Message, state: FSMContext):
    number = message.text.strip()
    success = await add_printer_to_db(number)
    if success:
        await message.answer(f"✅ Принтер #{number} добавлен в реестр.", reply_markup=main_menu(message.from_user.id))
    else:
        await message.answer(f"❌ Принтер с номером #{number} уже существует.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ---------- АДМИНКА: СНЯТЬ ПРИНТЕР ----------
async def menu_remove_printer(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🗑️ Введите номер принтера, который хотите снять с производства:")
    await state.set_state(AdminRemovePrinter.waiting_for_number)
    await callback.answer()

async def remove_printer_number(message: Message, state: FSMContext):
    number = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id, status FROM printers WHERE number = ?", (number,))
        row = await cursor.fetchone()
        if not row:
            await message.answer("❌ Принтер не найден.", reply_markup=main_menu(message.from_user.id))
        else:
            await db.execute("UPDATE printers SET status = 'снят' WHERE number = ?", (number,))
            await db.commit()
            await message.answer(f"✅ Принтер #{number} снят с производства.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ---------- АДМИНКА: ПРОСМОТР БАЗЫ ДЕТАЛЕЙ ----------
async def menu_parts_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT code, name, quantity_packed FROM parts ORDER BY code")
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.edit_text("📦 Склад пуст.", reply_markup=main_menu(callback.from_user.id))
        await callback.answer()
        return

    text = "📋 **Текущие складские остатки:**\n\n"
    for code, name, qty in rows:
        text += f"`{code}` — {name}: {qty} шт.\n"

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

# ---------- АДМИНКА: ОЧИСТИТЬ ОТГРУЖЕННЫЕ ----------
async def menu_ship(callback: CallbackQuery):
    # Сразу предлагаем список деталей с кнопками для отгрузки
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id, code, name, quantity_packed FROM parts WHERE quantity_packed > 0")
        parts = await cursor.fetchall()

    if not parts:
        await callback.message.edit_text("🚚 Нет деталей для отгрузки (склад пуст).",
                                         reply_markup=main_menu(callback.from_user.id))
        await callback.answer()
        return

    kb = InlineKeyboardBuilder()
    for part_id, code, name, qty in parts:
        kb.button(text=f"{code} ({qty} шт.) - всё", callback_data=f"ship_all_{part_id}")
        kb.button(text=f"{code} - частично", callback_data=f"ship_part_{part_id}")
    kb.button(text="🔙 Назад", callback_data="back_to_main")
    kb.adjust(2)
    await callback.message.edit_text("Выберите деталь для отгрузки:", reply_markup=kb.as_markup())
    await callback.answer()

async def ship_all(callback: CallbackQuery):
    part_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT code, name, quantity_packed FROM parts WHERE id = ?", (part_id,))
        part = await cursor.fetchone()
        if not part:
            await callback.answer("Ошибка: деталь не найдена.", show_alert=True)
            return
        code, name, qty = part
        # Логируем отгрузку
        await db.execute("INSERT INTO shipments (part_id, user_id, qty_shipped) VALUES (?, ?, ?)",
                         (part_id, user_id, qty))
        # Обнуляем
        await db.execute("UPDATE parts SET quantity_packed = 0 WHERE id = ?", (part_id,))
        await db.commit()

    await callback.message.edit_text(f"✅ Деталь {code} ({name}) отгружена полностью: {qty} шт.",
                                     reply_markup=main_menu(user_id))
    await callback.answer()

async def ship_part_start(callback: CallbackQuery, state: FSMContext):
    part_id = int(callback.data.split("_")[2])
    await state.update_data(part_id=part_id)
    await callback.message.edit_text("Введите количество, которое хотите отгрузить:")
    await state.set_state(AdminShipPart.waiting_for_qty)
    await callback.answer()

async def ship_part_qty(message: Message, state: FSMContext):
    try:
        qty = int(message.text)
        if qty <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное целое число.")
        return

    data = await state.get_data()
    part_id = data["part_id"]
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT code, name, quantity_packed FROM parts WHERE id = ?", (part_id,))
        part = await cursor.fetchone()
        if not part:
            await message.answer("❌ Деталь не найдена.", reply_markup=main_menu(user_id))
            await state.clear()
            return
        code, name, current_qty = part
        if qty > current_qty:
            await message.answer(f"❌ Недостаточно на складе. Доступно: {current_qty} шт.")
            return

        new_qty = current_qty - qty
        await db.execute("UPDATE parts SET quantity_packed = ? WHERE id = ?", (new_qty, part_id))
        await db.execute("INSERT INTO shipments (part_id, user_id, qty_shipped) VALUES (?, ?, ?)",
                         (part_id, user_id, qty))
        await db.commit()

    await message.answer(f"✅ Отгружено: {code} - {name}, {qty} шт. Осталось: {new_qty}.",
                         reply_markup=main_menu(user_id))
    await state.clear()

# ---------- УНИВЕРСАЛЬНЫЙ ВОЗВРАТ В ГЛАВНОЕ МЕНЮ ----------
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

# ---------- ПРОВЕРКА ПРАВ ДЛЯ АДМИН-КОМАНД ----------
async def admin_only_filter(callback: CallbackQuery) -> bool:
    """Фильтр, пропускающий только админов."""
    return await is_admin(callback.from_user.id)

# ---------- ЗАПУСК ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()

    bot = Bot(token=TOKEN)
    dp = Dispatcher()

    # Команда /start
    dp.message.register(start_command, Command("start"))

    # Обработчики меню (callback)
    dp.callback_query.register(menu_break, F.data == "menu_break")
    dp.callback_query.register(menu_return, F.data == "menu_return")
    dp.callback_query.register(menu_list_broken, F.data == "menu_list_broken")
    dp.callback_query.register(menu_add_part, F.data == "menu_add_part")
    dp.callback_query.register(back_to_main, F.data == "back_to_main")

    # Админские меню
    dp.callback_query.register(menu_stats, F.data == "menu_stats", admin_only_filter)
    dp.callback_query.register(menu_add_printer, F.data == "menu_add_printer", admin_only_filter)
    dp.callback_query.register(menu_remove_printer, F.data == "menu_remove_printer", admin_only_filter)
    dp.callback_query.register(menu_parts_list, F.data == "menu_parts_list", admin_only_filter)
    dp.callback_query.register(menu_ship, F.data == "menu_ship", admin_only_filter)

    # Возврат принтера в работу
    dp.callback_query.register(return_printer, F.data.startswith("return_"))
    # Отгрузка
    dp.callback_query.register(ship_all, F.data.startswith("ship_all_"), admin_only_filter)
    dp.callback_query.register(ship_part_start, F.data.startswith("ship_part_"), admin_only_filter)
    # История поломок
    dp.callback_query.register(stat_history_start, F.data == "stat_history", admin_only_filter)

    # FSM: Сломался принтер
    dp.message.register(break_number, PrinterBreak.waiting_for_number)
    dp.message.register(break_reason, PrinterBreak.waiting_for_reason)
    # FSM: Добавление детали
    dp.message.register(part_code, PartAdd.waiting_for_code)
    dp.message.register(part_name, PartAdd.waiting_for_name)
    dp.message.register(part_qty, PartAdd.waiting_for_qty)
    # FSM: Админ - добавление принтера
    dp.message.register(add_printer_number, AdminAddPrinter.waiting_for_number)
    # FSM: Админ - снятие принтера
    dp.message.register(remove_printer_number, AdminRemovePrinter.waiting_for_number)
    # FSM: Админ - история
    dp.message.register(stat_history_show, AdminStatHistory.waiting_for_printer_number)
    # FSM: Админ - частичная отгрузка
    dp.message.register(ship_part_qty, AdminShipPart.waiting_for_qty)

    # Периодическая задача: поддержание WebSocket (для Render)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
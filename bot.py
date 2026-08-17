from __future__ import annotations

import asyncio
import csv
import io
import logging
from enum import Enum
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings, load_settings
from app.database import Applicant, Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class Registration(StatesGroup):
    first_name = State()
    last_name = State()


class Status(str, Enum):
    filling = "filling"
    registered = "registered"
    join_requested = "join_requested"
    approved = "approved"
    joined = "joined"
    rejected = "rejected"
    left = "left"


STATUS_LABELS = {
    Status.filling: "заполняет анкету",
    Status.registered: "анкета заполнена",
    Status.join_requested: "ожидает решения",
    Status.approved: "одобрен",
    Status.joined: "состоит в группе",
    Status.rejected: "отклонён",
    Status.left: "вышел из группы",
}


def clean_name(value: str) -> str | None:
    value = " ".join(value.strip().split())
    if not 2 <= len(value) <= 80 or any(not (char.isalpha() or char in " -'") for char in value):
        return None
    return value


def join_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подать заявку в группу", url=url)]])


def moderation_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"moderate:approve:{telegram_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"moderate:reject:{telegram_id}"),
            ]
        ]
    )


def applicant_card(applicant: Applicant) -> str:
    username = f"@{escape(applicant.username)}" if applicant.username else "не указан"
    profile_name = " ".join(
        part for part in (applicant.telegram_first_name, applicant.telegram_last_name) if part
    ) or "не указано"
    return (
        "<b>Заявка в группу</b>\n"
        f"ФИО: <b>{escape(applicant.full_name)}</b>\n"
        f"Telegram ID: <code>{applicant.telegram_id}</code>\n"
        f"Username: {username}\n"
        f"Имя в профиле: {escape(profile_name)}\n"
        f"Статус: {STATUS_LABELS[Status(applicant.status)]}"
    )


def is_active_member(status: ChatMemberStatus, is_member: bool | None) -> bool:
    return status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER} or (
        status == ChatMemberStatus.RESTRICTED and bool(is_member)
    )


def build_router(settings: Settings, database: Database) -> Router:
    router = Router()

    async def notify_admins(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text, reply_markup=markup)
            except TelegramForbiddenError:
                logger.warning("Администратор %s ещё не открыл диалог с ботом", admin_id)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        assert message.from_user
        existing = database.get(message.from_user.id)
        if existing and existing.status in {Status.join_requested, Status.approved, Status.joined}:
            await message.answer(f"Ваша анкета уже есть в системе. Статус: <b>{STATUS_LABELS[Status(existing.status)]}</b>.")
            return
        if existing and existing.status == Status.registered:
            await message.answer("Анкета уже заполнена. Нажмите кнопку, чтобы подать заявку в группу.")
            await issue_invite(message, message.from_user.id)
            return

        database.begin_form(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        await state.set_state(Registration.first_name)
        await message.answer("Здравствуйте! Для заявки в закрытую группу укажите ваше <b>имя</b>.")

    @router.message(Registration.first_name)
    async def receive_first_name(message: Message, state: FSMContext) -> None:
        assert message.from_user
        name = clean_name(message.text or "")
        if not name:
            await message.answer("Введите имя от 2 до 80 символов: только буквы, пробел, дефис или апостроф.")
            return
        database.save_first_name(message.from_user.id, name)
        await state.set_state(Registration.last_name)
        await message.answer("Теперь укажите вашу <b>фамилию</b>.")

    @router.message(Registration.last_name)
    async def receive_last_name(message: Message, state: FSMContext) -> None:
        assert message.from_user
        surname = clean_name(message.text or "")
        if not surname:
            await message.answer("Введите фамилию от 2 до 80 символов: только буквы, пробел, дефис или апостроф.")
            return
        applicant = database.submit_form(message.from_user.id, surname)
        await state.clear()
        await message.answer(f"Спасибо, <b>{applicant.full_name}</b>. Теперь можно отправить заявку в группу.")
        await issue_invite(message, message.from_user.id)

    async def issue_invite(message: Message, telegram_id: int) -> None:
        applicant = database.get(telegram_id)
        if applicant is None:
            await message.answer("Сначала заполните анкету командой /start.")
            return
        if applicant.invite_link:
            await message.answer("Используйте эту ссылку для отправки заявки:", reply_markup=join_keyboard(applicant.invite_link))
            return
        try:
            invite = await message.bot.create_chat_invite_link(
                chat_id=settings.target_chat_id,
                name=f"app-{telegram_id}",
                creates_join_request=True,
            )
        except TelegramBadRequest as error:
            logger.exception("Не удалось создать ссылку")
            await message.answer("Не удалось подготовить заявку. Сообщите администратору.")
            await notify_admins(message.bot, f"⚠️ Не создана ссылка для <code>{telegram_id}</code>: {error}")
            return
        database.save_invite_link(telegram_id, invite.invite_link)
        await message.answer("Нажмите кнопку. Telegram попросит отправить заявку; после этого администратор увидит вашу анкету.", reply_markup=join_keyboard(invite.invite_link))

    @router.chat_join_request()
    async def on_join_request(request: ChatJoinRequest) -> None:
        if request.chat.id != settings.target_chat_id:
            return
        applicant = database.mark_join_requested(request.from_user.id)
        if applicant is None:
            await request.bot.decline_chat_join_request(settings.target_chat_id, request.from_user.id)
            logger.info("Отклонена заявка без анкеты: %s", request.from_user.id)
            return
        await notify_admins(request.bot, applicant_card(applicant), moderation_keyboard(applicant.telegram_id))

    @router.callback_query(F.data.startswith("moderate:"))
    async def moderate(callback: CallbackQuery) -> None:
        assert callback.from_user and callback.data and callback.message
        if callback.from_user.id not in settings.admin_ids:
            await callback.answer("У вас нет прав для модерации.", show_alert=True)
            return
        _, action, raw_id = callback.data.split(":")
        if action not in {"approve", "reject"}:
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        telegram_id = int(raw_id)
        applicant = database.get(telegram_id)
        if applicant is None or applicant.status != Status.join_requested:
            await callback.answer("Эта заявка уже обработана.", show_alert=True)
            return
        try:
            if action == "approve":
                await callback.bot.approve_chat_join_request(settings.target_chat_id, telegram_id)
                applicant = database.mark_approved(telegram_id, callback.from_user.id)
                result = "✅ Одобрено"
            else:
                await callback.bot.decline_chat_join_request(settings.target_chat_id, telegram_id)
                applicant = database.mark_rejected(telegram_id, callback.from_user.id)
                result = "❌ Отклонено"
        except TelegramBadRequest:
            await callback.answer("Telegram уже обработал эту заявку.", show_alert=True)
            return
        assert applicant
        await callback.message.edit_text(f"{applicant_card(applicant)}\n\n<b>{result}</b>")
        await callback.answer(result)

    @router.chat_member()
    async def on_chat_member(update: ChatMemberUpdated) -> None:
        if update.chat.id != settings.target_chat_id:
            return
        old_active = is_active_member(update.old_chat_member.status, getattr(update.old_chat_member, "is_member", None))
        new_active = is_active_member(update.new_chat_member.status, getattr(update.new_chat_member, "is_member", None))
        if old_active == new_active:
            return
        applicant = database.mark_member(update.new_chat_member.user.id, new_active)
        if applicant:
            action = "вступил(а) в группу" if new_active else "вышел(ла) из группы"
            await notify_admins(update.bot, f"ℹ️ <b>{applicant.full_name}</b> {action}.")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if not message.from_user or message.from_user.id not in settings.admin_ids:
            return
        counts = database.counts()
        order = (Status.filling, Status.registered, Status.join_requested, Status.approved, Status.joined, Status.rejected, Status.left)
        lines = [f"{STATUS_LABELS[item]}: <b>{counts.get(item.value, 0)}</b>" for item in order]
        await message.answer("<b>Статусы анкет</b>\n" + "\n".join(lines))

    @router.message(Command("applications"))
    async def applications(message: Message) -> None:
        if not message.from_user or message.from_user.id not in settings.admin_ids:
            return
        applicants = database.all_applicants()
        if not applicants:
            await message.answer("Анкет пока нет.")
            return
        lines = ["<b>Последние анкеты</b>"]
        for applicant in applicants[:50]:
            lines.append(f"• {applicant.full_name} — {STATUS_LABELS[Status(applicant.status)]} (<code>{applicant.telegram_id}</code>)")
        if len(applicants) > 50:
            lines.append(f"\nПоказаны 50 из {len(applicants)}. Полный список: /export")
        await message.answer("\n".join(lines))

    @router.message(Command("export"))
    async def export(message: Message) -> None:
        if not message.from_user or message.from_user.id not in settings.admin_ids:
            return
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Фамилия", "Имя", "Telegram ID", "Username", "Статус", "Анкета", "Заявка", "Одобрено", "Вступил", "Вышел"])
        for applicant in database.all_applicants():
            writer.writerow([
                applicant.last_name or "", applicant.first_name or "", applicant.telegram_id,
                applicant.username or "", STATUS_LABELS[Status(applicant.status)], applicant.submitted_at or "",
                applicant.join_requested_at or "", applicant.approved_at or "", applicant.joined_at or "", applicant.left_at or "",
            ])
        document = BufferedInputFile(stream.getvalue().encode("utf-8-sig"), filename="applicants.csv")
        await message.answer_document(document, caption="Реестр заявок")

    return router


async def main() -> None:
    settings = load_settings()
    database = Database(settings.database_path)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router(settings, database))
    try:
        await dispatcher.start_polling(bot)
    finally:
        database.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

import copy
import io
import re
from functools import wraps
import multiprocessing as mp
import cv2

from settings import Settings
from utils import mainlogger
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, MessageHandler, ConversationHandler,
                          ContextTypes, filters)

## Enable logging
# logging.basicConfig(
#     format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
# )
# # set higher logging level for httpx to avoid all GET and POST requests being logged
# logging.getLogger("httpx").setLevel(logging.WARNING)
#
# logger = mainlogger


def restricted_to_admin(func):
    @wraps(func)
    async def wrapped(self, update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in Settings.telegram_superadminlist + Settings.telegram_adminlist:
            print(f"Unauthorized access denied for {user_id} on {func.__name__}.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapped

def restricted_to_user(func):
    @wraps(func)
    async def wrapped(self, update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in Settings.telegram_superadminlist + Settings.telegram_adminlist + Settings.telegram_userlist:
            print(f"Unauthorized access denied for {user_id} on {func.__name__}.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapped

class Telegrambot(mp.Process):

    def __init__(self, streaminfos, dbupdatequeue):
        mainlogger.info(f'Starting Telegrambot')
        super().__init__()
        self.streaminfos: dict = streaminfos
        self.dbupdatequeue = dbupdatequeue
        self.bot = None
        self.userkeyboard = ReplyKeyboardMarkup([['/start']], is_persistent=True)
        self.adminkeyboard = ReplyKeyboardMarkup([['/start'],['/admin','exit admin']], is_persistent=True)

    def create_arm_disarm_keyboard(self):
        keyboard = []
        for streamid in self.streaminfos.keys()        :
            if self.streaminfos[streamid]['armed'].value:
                text = 'Disarm'
            else:
                text = 'Arm'
            if streamid == 0:
                text += ' All'
            else:
                text += f' Stream {streamid}'
            streamkeyboard = [InlineKeyboardButton(text, callback_data=f'arm_disarm_{streamid}')]
            keyboard.append(streamkeyboard)
        return keyboard

    def create_take_snapshot_keyboard(self):
        keyboard = []
        for streamid in self.streaminfos.keys():
            text = 'Snapshot for'
            if streamid == 0:
                text += ' All'
            else:
                text += f' Stream {streamid}'
            streambutton = [InlineKeyboardButton(text, callback_data=f'take_snapshot_{streamid}')]
            keyboard.append(streambutton)
        return keyboard

    @restricted_to_admin
    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        keyboard = [
            [InlineKeyboardButton('User Management', callback_data=f'user_management_show')],
            [InlineKeyboardButton('Stream Management', callback_data=f'stream_management_show')],
            [InlineKeyboardButton('System Settings', callback_data=f'system_settings_show')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text("Choose an action:", reply_markup=reply_markup)
        return 'inline_keyboard'

    @restricted_to_admin
    async def admin_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(f'Exited admin interface',)
        return ConversationHandler.END

    @restricted_to_admin
    async def user_management_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        keyboard = [
            [
                InlineKeyboardButton('Add Admin', callback_data=f'user_management_add_admin_show'),
                InlineKeyboardButton('Remove Admin', callback_data=f'user_management_remove_admin_show'),
             ],
            [
                InlineKeyboardButton('Add User', callback_data=f'user_management_add_user_show'),
                InlineKeyboardButton('Remove User', callback_data=f'user_management_remove_user_show'),
            ],
            [
                InlineKeyboardButton('Add Alarm User', callback_data=f'user_management_add_alarm_user_show'),
                InlineKeyboardButton('Remove Alarm User', callback_data=f'user_management_remove_alarm_user_show'),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose an option'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_admin
    async def user_management_add_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        # Callback origin
        if update.message is None:
            query = update.callback_query
            await query.answer()
            await update.effective_message.edit_text('Entering Text Mode')
        # Message Origin
        else:
            try:
                adminid = int(update.message.text)
            except Exception as e:
                reply_str = f'{e} \n Please try again'
                await update.effective_message.reply_text(text=reply_str)
                return 'add_admin_text_input'
            Settings.telegram_adminlist.append(adminid)
        reply_str = 'Current admins are \n ******\n'
        for id in Settings.telegram_adminlist:
            reply_str += f'{id}\n'
        reply_str += f'******\nType an additional id to add'
        await update.effective_message.reply_text(text=reply_str)
        return 'add_admin_text_input'

    @restricted_to_admin
    async def user_management_remove_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        command = re.match(re.compile('^(user_management_remove_admin_)(.*)$'), query.data).group(2)
        if command != 'show':
            Settings.telegram_adminlist.remove(int(command))
        keyboard = [
            [InlineKeyboardButton(f'Remove {adminid}', callback_data=f'user_management_remove_admin_{adminid}'),]
            for adminid in Settings.telegram_adminlist
        ]
        keyboard += [[InlineKeyboardButton(f'Back to User Management', callback_data=f'user_management_show')]]

        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose an admin to remove'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_admin
    async def user_management_add_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        # Callback origin
        if update.message is None:
            query = update.callback_query
            await query.answer()
            await update.effective_message.edit_text('Entering Text Mode')
        # Message Origin
        else:
            try:
                userid = int(update.message.text)
            except Exception as e:
                reply_str = f'{e} \n Please try again'
                await update.effective_message.reply_text(text=reply_str)
                return 'add_user_text_input'
            Settings.telegram_userlist.append(userid)
        reply_str = 'Current users are \n ******\n'
        for id in Settings.telegram_userlist:
            reply_str += f'{id}\n'
        reply_str += f'******\nType an additional id to add'
        await update.effective_message.reply_text(text=reply_str)
        return 'add_user_text_input'

    @restricted_to_admin
    async def user_management_remove_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        command = re.match(re.compile('^(user_management_remove_user_)(.*)$'), query.data).group(2)
        if command != 'show':
            Settings.telegram_userlist.remove(int(command))
        keyboard = [
            [InlineKeyboardButton(f'Remove {userid}', callback_data=f'user_management_remove_user_{userid}'),]
            for userid in Settings.telegram_userlist
        ]
        keyboard += [[InlineKeyboardButton(f'Back to User Management', callback_data=f'user_management_show')]]

        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose a user to remove'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_admin
    async def user_management_add_alarm_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        # Callback origin
        if update.message is None:
            query = update.callback_query
            await query.answer()
            await update.effective_message.edit_text('Entering Text Mode')
        # Message Origin
        else:
            try:
                alarmuserid = int(update.message.text)
            except Exception as e:
                reply_str = f'{e} \n Please try again'
                await update.effective_message.reply_text(text=reply_str)
                return 'add_alarm_user_text_input'
            Settings.telegram_alarmlist.append(alarmuserid)
        reply_str = 'Current alarm users are \n ******\n'
        for id in Settings.telegram_alarmlist:
            reply_str += f'{id}\n'
        reply_str += f'******\nType an additional id to add'
        await update.effective_message.reply_text(text=reply_str)
        return 'add_alarm_user_text_input'

    @restricted_to_admin
    async def user_management_remove_alarm_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        command = re.match(re.compile('^(user_management_remove_alarm_user_)(.*)$'), query.data).group(2)
        if command != 'show':
            Settings.telegram_alarmlist.remove(int(command))
        keyboard = [
            [InlineKeyboardButton(f'Remove {userid}', callback_data=f'user_management_remove_alarm_user_{userid}'),]
            for userid in Settings.telegram_alarmlist
        ]
        keyboard += [[InlineKeyboardButton(f'Back to User Management', callback_data=f'user_management_show')]]

        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose an alarm user to remove'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_admin
    async def stream_management_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        keyboard = [
                [InlineKeyboardButton('Add Stream', callback_data=f'stream_management_add_stream_show'),],
                [InlineKeyboardButton('Remove Stream', callback_data=f'stream_management_remove_stream_show'),],
                [InlineKeyboardButton('Edit Stream', callback_data=f'stream_management_edit_stream_show'),],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose an option'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_user
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        keyboard = [
            [InlineKeyboardButton('Arm/Disarm', callback_data=f'arm_disarm_show')],
            [InlineKeyboardButton('Snapshots', callback_data=f'take_snapshot_show')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.effective_user.id in Settings.telegram_adminlist:
            reply_keyboard_markup = self.adminkeyboard
        elif update.effective_user.id in Settings.telegram_userlist:
            reply_keyboard_markup = self.userkeyboard
        else:
            reply_keyboard_markup = ReplyKeyboardRemove()
        await update.effective_message.reply_text('Welcome to the user interface', reply_markup=reply_keyboard_markup)
        await update.effective_message.reply_text("Choose an action:", reply_markup=reply_markup)
        return ConversationHandler.END

    @restricted_to_user
    async def arm_disarm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Parses the CallbackQuery and updates the message text."""
        query = update.callback_query
        await query.answer()
        command = re.match(re.compile('^(arm_disarm_)(.*)$'), query.data).group(2)
        if command == 'show':
            reply_str = 'Choose an action'
        else:
            streamid = int(command)
            curr_state = self.streaminfos[streamid]['armed'].value
            if curr_state:
                reply_str = 'Disarmed'
                self.streaminfos[streamid]['armed'].value = 0
                self.dbupdatequeue.put('Update')
            else:
                reply_str = 'Armed'
                self.streaminfos[streamid]['armed'].value = 1
                self.dbupdatequeue.put('Update')
            if streamid == 0:
                reply_str += ' All'
            else:
                reply_str += f' Stream {streamid}'
            mainlogger.info(reply_str)
        reply_markup = InlineKeyboardMarkup(self.create_arm_disarm_keyboard())
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_user
    async def take_snapshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        command = re.match(re.compile('^(take_snapshot_)(.*)$'), query.data).group(2)
        if command == 'show':
            reply_str = 'Choose an action'
            reply_markup = InlineKeyboardMarkup(self.create_take_snapshot_keyboard())
            await query.edit_message_text(text=reply_str, reply_markup=reply_markup)
        else:
            streamid = int(command)
            if streamid == 0:
                streamids = [x for x in self.streaminfos.keys()][1:]
            else:
                streamids = [streamid]
            for stream in streamids:
                img = self.streaminfos[stream]['framebuffer'][-1]
                # encode
                is_success, buffer = cv2.imencode(".jpg", img)
                io_buf = io.BytesIO(buffer)
                await update.effective_message.reply_photo(io_buf, f'Stream {stream}')
            await self.start_command(update, context)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Displays info on how to use the bot."""
        non_user_reply_str = 'Please contact an admin to get access to this bot'
        user_reply_str = 'Use /start to use this bot'
        admin_reply_str = user_reply_str + ''
        if update.effective_user.id in Settings.telegram_adminlist:
            reply_str = admin_reply_str
            reply_markup = self.adminkeyboard
        elif update.effective_user.id in Settings.telegram_userlist:
            reply_str = user_reply_str
            reply_markup = self.userkeyboard
        else:
            reply_str = non_user_reply_str
            reply_markup = ReplyKeyboardRemove()
        await update.effective_message.reply_text(reply_str, reply_markup=reply_markup)

    def run(self) -> None:
        """Run the bot."""
        # Create the Application and pass it your bot's token.
        self.bot = Application.builder().token(Settings.fractal_token).build()

        adminconversation = ConversationHandler(
            entry_points=[
                CommandHandler("admin", self.admin_command),
            ],
            states={
                'inline_keyboard': [
                    CallbackQueryHandler(self.user_management_entry, pattern='^user_management_show$'),
                    CallbackQueryHandler(self.user_management_add_admin, pattern='^user_management_add_admin_show$'),
                    CallbackQueryHandler(self.user_management_remove_admin,pattern='^user_management_remove_admin_.*$'),
                    CallbackQueryHandler(self.user_management_add_user, pattern='^user_management_add_user_show$'),
                    CallbackQueryHandler(self.user_management_remove_user,pattern='^user_management_remove_user_.*$'),
                    CallbackQueryHandler(self.user_management_add_alarm_user, pattern='^user_management_add_alarm_user_show$'),
                    CallbackQueryHandler(self.user_management_remove_alarm_user, pattern='^user_management_remove_alarm_user_.*$'),
                    CallbackQueryHandler(self.stream_management_entry, pattern='^stream_management_show$'),
                ],
                'add_admin_text_input': [
                    MessageHandler(filters.TEXT & ~(filters.COMMAND | filters.Regex("^exit admin$")),
                                   self.user_management_add_admin),
                ],
                'add_user_text_input': [
                    MessageHandler(filters.TEXT & ~(filters.COMMAND | filters.Regex("^exit admin$")),
                                   self.user_management_add_user),
                ],
                'add_alarm_user_text_input': [
                    MessageHandler(filters.TEXT & ~(filters.COMMAND | filters.Regex("^exit admin$")),
                                   self.user_management_add_alarm_user),
                ],
            },
            fallbacks=[
                MessageHandler(filters.Regex("^exit admin$"), self.admin_done),
                CommandHandler("admin", self.admin_command),
                CommandHandler("start", self.start_command),
                CommandHandler("help", self.help_command),
            ],
        )

        self.bot.add_handler(adminconversation)
        self.bot.add_handler(CommandHandler("start", self.start_command))
        self.bot.add_handler(CallbackQueryHandler(self.arm_disarm, pattern='^arm_disarm_.*$'))
        self.bot.add_handler(CallbackQueryHandler(self.take_snapshot, pattern='^take_snapshot_.*$'))
        self.bot.add_handler(CommandHandler("help", self.help_command))
        self.bot.add_handler(MessageHandler(filters.ALL, self.help_command))

        # Run the bot until the user presses Ctrl-C
        self.bot.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    dict = {
        0:{'armed':mp.Value('i', 1)},
        1:{'armed':mp.Value('i', 1)}
    }
    bot = Telegrambot(dict, mp.Queue())
    bot.run()

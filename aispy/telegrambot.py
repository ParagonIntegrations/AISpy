import copy
import io
import multiprocessing
import re
from functools import wraps

import cv2

from settings import Settings
from utils import mainlogger
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters

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
        if user_id not in Settings.telegram_adminlist:
            print(f"Unauthorized access denied for {user_id}.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapped

def restricted_to_user(func):
    @wraps(func)
    async def wrapped(self, update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in Settings.telegram_userlist + Settings.telegram_adminlist:
            print(f"Unauthorized access denied for {user_id}.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapped

class Telegrambot(multiprocessing.Process):

    def __init__(self, streaminfos, dbupdatequeue):
        mainlogger.info(f'Starting Telegrambot')
        super().__init__()
        self.streaminfos: dict = streaminfos
        self.dbupdatequeue = dbupdatequeue

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

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton('Arm/Disarm', callback_data=f'arm_disarm_show')],
            [InlineKeyboardButton('Snapshots', callback_data=f'take_snapshot_show')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text("Choose an action:", reply_markup=reply_markup)

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
                reply_str += f'Stream {streamid}'
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
        await update.message.reply_text("Use /start to use this bot.")

    def run(self) -> None:
        """Run the bot."""
        # Create the Application and pass it your bot's token.
        application = Application.builder().token(Settings.fractal_token).build()

        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CallbackQueryHandler(self.arm_disarm, pattern='^arm_disarm_.*$'))
        application.add_handler(CallbackQueryHandler(self.take_snapshot, pattern='^take_snapshot_.*$'))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(MessageHandler(filters.ALL, self.start_command))

        # Run the bot until the user presses Ctrl-C
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    dict = {
        0:{'arm':1},
        1:{'arm':1}
    }
    bot = Telegrambot(dict)
    bot.run()

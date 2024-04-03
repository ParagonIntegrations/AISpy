import multiprocessing
from settings import Settings
from utils import mainlogger
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

# # Enable logging
# logging.basicConfig(
#     format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
# )
# # set higher logging level for httpx to avoid all GET and POST requests being logged
# logging.getLogger("httpx").setLevel(logging.WARNING)
#
# logger = mainlogger

class Telegrambot(multiprocessing.Process):

    def __init__(self, streaminfos, dbupdatequeue):
        mainlogger.info(f'Starting Telegrambot')
        super().__init__()
        self.streaminfos: dict = streaminfos
        self.dbupdatequeue = dbupdatequeue


    def create_keyboard(self):
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
            streamkeyboard = [InlineKeyboardButton(text, callback_data=f'{streamid}')]
            keyboard.append(streamkeyboard)
        return keyboard

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        reply_markup = InlineKeyboardMarkup(self.create_keyboard())
        await update.message.reply_text("Choose an action:", reply_markup=reply_markup)


    async def arm_disarm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Parses the CallbackQuery and updates the message text."""
        query = update.callback_query
        await query.answer()
        streamid = int(query.data)
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
        reply_markup = InlineKeyboardMarkup(self.create_keyboard())

        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)


    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Displays info on how to use the bot."""
        await update.message.reply_text("Use /start to use this bot.")


    def run(self) -> None:
        """Run the bot."""
        # Create the Application and pass it your bot's token.
        application = Application.builder().token(Settings.fractal_token).build()

        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CallbackQueryHandler(self.arm_disarm))
        application.add_handler(CommandHandler("help", self.help_command))

        # Run the bot until the user presses Ctrl-C
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    dict = {
        0:{'arm':1},
        1:{'arm':1}
    }
    bot = Telegrambot(dict)
    bot.run()

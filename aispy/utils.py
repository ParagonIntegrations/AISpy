import asyncio
import logging
import time
from logging import Handler, Formatter
from logging.handlers import RotatingFileHandler

import telegram
from telegram import InlineKeyboardButton

from settings import Settings
import requests
import datetime

# Create a rotating logger
def create_rotating_log(path, logsize, lognum, file_level, console_level, telegram_level, telegram_id, telegram_token):
	# Create the logger
	logger = logging.getLogger("Main Logger")
	logger.setLevel(logging.DEBUG)
	# Create a rotating filehandler
	filehandler = RotatingFileHandler(path, maxBytes=logsize, backupCount=lognum)
	filehandler.setLevel(file_level)
	# Create a streamhandler to print to console
	consolehandler = logging.StreamHandler()
	consolehandler.setLevel(console_level)
	# Create a formatter and add to filehandler and consolehandler
	formatter = logging.Formatter('%(asctime)s-%(name)s-%(levelname)s-%(funcName)s - %(message)s')
	filehandler.setFormatter(formatter)
	consolehandler.setFormatter(formatter)
	# Create the requestshandler to send to Telegram
	telegramhandler = TelegramRequestsHandler(telegram_id, telegram_token)
	telegramhandler.setLevel(telegram_level)
	telegramformatter = TelegramFormatter()
	telegramhandler.setFormatter(telegramformatter)
	# Add the filehandler and consolehandler to the logger
	logger.addHandler(filehandler)
	logger.addHandler(consolehandler)
	logger.addHandler(telegramhandler)
	return logger

class TelegramRequestsHandler(Handler):

	def __init__(self, telegram_id, telegram_token):
		super(TelegramRequestsHandler, self).__init__()
		self.telegram_id = telegram_id
		self.telgram_token = telegram_token

	def emit(self, record):
		log_entry = self.format(record)
		payload = {
			'chat_id': self.telegram_id,
			'text': log_entry,
			'parse_mode': 'HTML'
		}
		return requests.post("https://api.telegram.org/bot{token}/sendMessage".format(token=self.telgram_token),
							 data=payload).content

class TelegramFormatter(Formatter):
	def __init__(self):
		super(TelegramFormatter, self).__init__()

	def format(self, record):
		t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
		msg = record.msg
		if record.exc_text:
			msg += '\n' + record.exc_text
		return "<i>{datetime}</i><pre>\n{message}</pre>".format(message=msg, datetime=t)

mainlogger = create_rotating_log(Settings.log_name,
								 Settings.log_maxbytes,
								 Settings.log_maxnum,
								 Settings.file_loglevel,
								 Settings.console_loglevel,
								 Settings.telegram_loglevel,
								 Settings.telegram_chat_id,
								 Settings.telegram_token)


def send_photo_telegram(image_path, chat_ids, token, image_caption=""):
	for chat_id in chat_ids:
		data = {"chat_id": chat_id, "caption": image_caption}
		url = f'https://api.telegram.org/bot{token}/sendPhoto?chat_id={chat_id}'
		with open(image_path, "rb") as image_file:
			ret = requests.post(url, data=data, files={"photo": image_file})
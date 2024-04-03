import sqlite3
import os
from utils import mainlogger
from settings import UserSettings
import copy

class DBDriver:
	def __init__(self, dbfile):
		self.db = dbfile
		self.checkdb()
		self.conn = sqlite3.connect(self.db, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)

	def checkdb(self) -> None:
		# Check if the db exists
		if os.path.isfile(self.db):
			mainlogger.debug(f'Database already exists')
		else:
			mainlogger.warning(f'No database file found at {self.db}')
			self.create_database()

	def create_database(self) -> None:
		mainlogger.info(f'Creating database {self.db}')
		conn = sqlite3.connect(self.db)
		c = conn.cursor()
		c.execute('''
			CREATE TABLE streaminfos(
				id INTEGER PRIMARY KEY NOT NULL,
				streamid INTEGER NOT NULL,
				armed INTEGER,
				url TEXT,
				dimensions TEXT,
				detect INTEGER,
				detection_classes TEXT,
				confidence_threshold REAL,
				record INTEGER,
				detectarea TEXT,
				recordcounter INTEGER
			)
		''')
		# Populate with the stream data from settings
		conn.commit()
		for streamid, steamdict in UserSettings.streaminfo.items():
			c.execute('''
				INSERT INTO streaminfos(
					streamid,
					armed
				) VALUES (?, ?)
			''',(streamid, steamdict['armed']))
		conn.commit()
		conn.close()

	def save_state(self, statedict: dict):
		mainlogger.info('Saving state to db')
		c = self.conn.cursor()
		for streamid, streamdict in statedict.items():
			c.execute('''
				UPDATE streaminfos SET armed = ?
				WHERE streamid = ?
			''', (streamdict['armed'], streamid))
		self.conn.commit()


	def load_state(self) -> dict:
		mainlogger.info('Loading state from db')
		returndict = copy.deepcopy(UserSettings.streaminfo)
		c = self.conn.cursor()
		streams = c.execute('''
			SELECT streamid, armed FROM streaminfos
		''')
		for stream in streams:
			streamid = stream[0]
			armed = stream[1]
			returndict[streamid]['armed'] = armed
		return returndict


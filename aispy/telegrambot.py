import asyncio
import copy
import datetime
import io
import math
import os
import re
import time
from functools import wraps
import multiprocessing as mp
import threading
import cv2
import requests

from settings import Settings, UserSettings
from utils import mainlogger
import logging
from autoarm import AutoArm, last_scheduled_action
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

def restricted_to_alarmuser(func):
    @wraps(func)
    async def wrapped(self, update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in Settings.telegram_alarmlist:
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
        self.application: Application | None = None
        # The Application's event loop, published by post_init while it is connected.
        self.app_loop: asyncio.AbstractEventLoop | None = None
        self.userkeyboard = ReplyKeyboardMarkup([['/start']], is_persistent=True)
        self.adminkeyboard = ReplyKeyboardMarkup([['/start'],['/admin','exit admin']], is_persistent=True)
        self.usetimer = True
        # Startup arm/disarm message, waiting for Telegram to come up (see post_init).
        self.startup_notification: asyncio.Task | None = None

    def create_arm_disarm_keyboard(self):
        buttons_per_row = 2
        num_buttons = len(self.streaminfos.keys())
        num_rows = math.ceil((num_buttons + buttons_per_row - 1)/buttons_per_row)
        keyboard = [[] for x in range(num_rows)]
        for i in range(num_buttons):
            streamid = list(self.streaminfos.keys())[i]
            if self.streaminfos[streamid]['armed'].value:
                text = 'Disarm'
            else:
                text = 'Arm'
            if streamid == 0:
                text += ' All'
            else:
                text += f' Stream {streamid}'
            streambutton = [InlineKeyboardButton(text, callback_data=f'arm_disarm_{streamid}')]
            row = (i+buttons_per_row-1)//buttons_per_row
            keyboard[row] += streambutton
        empty_buttons = (num_rows*buttons_per_row - (num_buttons + buttons_per_row - 1))
        empties = [InlineKeyboardButton('Empty Stream', callback_data='None') for x in range(empty_buttons)]
        keyboard[-1] += empties
        return keyboard

    def create_take_snapshot_keyboard(self):
        buttons_per_row = 2
        num_buttons = len(self.streaminfos.keys())
        num_rows = math.ceil((num_buttons + buttons_per_row - 1) / buttons_per_row)
        keyboard = [[] for x in range(num_rows)]
        for i in range(num_buttons):
            streamid = list(self.streaminfos.keys())[i]
            text = 'Snapshot for'
            if streamid == 0:
                text += ' All'
            else:
                text += f' Stream {streamid}'
            streambutton = [InlineKeyboardButton(text, callback_data=f'take_snapshot_{streamid}')]
            row = (i+buttons_per_row-1)//buttons_per_row
            keyboard[row] += streambutton
        empty_buttons = (num_rows * buttons_per_row - (num_buttons + buttons_per_row - 1))
        empties = [InlineKeyboardButton('Empty Stream', callback_data='None') for x in range(empty_buttons)]
        keyboard[-1] += empties
        return keyboard

    @restricted_to_admin
    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        keyboard = [
            [InlineKeyboardButton('User Management', callback_data=f'user_management_show')],
            [InlineKeyboardButton('Stream Management', callback_data=f'stream_management_show')],
            [InlineKeyboardButton('System Settings', callback_data=f'system_management_show')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text("Choose an action:", reply_markup=reply_markup)
        return 'inline_keyboard'

    @restricted_to_admin
    async def admin_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(f'Exited admin interface',)
        return ConversationHandler.END

    # User Management Section
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

    # Stream Management Section
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

    # System Management Section
    @restricted_to_admin
    async def system_management_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        keyboard = [
                [InlineKeyboardButton('Restart', callback_data=f'restart_docker'),],
                [InlineKeyboardButton('Timers', callback_data=f'timer_management_show'),]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose an option'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_admin
    async def restart_docker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        reply_str = 'Restarting'
        await query.edit_message_text(text=reply_str)
        os.kill(1, 9)

    def create_timer_keyboard(self):
        if self.usetimer:
            button = [InlineKeyboardButton('Disable Timers', callback_data=f'timer_management_disable_0'), ]
        else:
            button = [InlineKeyboardButton('Enable Timers', callback_data=f'timer_management_enable_0'), ]
        return button

    @restricted_to_admin
    async def timer_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        keyboard = [self.create_timer_keyboard()]
        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose an option'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_admin
    async def enable_timer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        timer_number = int(re.match(re.compile('^(timer_management_enable_)(.*)$'), query.data).group(2))
        if timer_number == 0:
            self.usetimer = True
            reply_str = f'Enabled All Timers'
        else:
            reply_str = f'Enabled Timer {timer_number}'
        keyboard = [self.create_timer_keyboard()]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    @restricted_to_admin
    async def disable_timer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        timer_number = int(re.match(re.compile('^(timer_management_disable_)(.*)$'), query.data).group(2))
        if timer_number == 0:
            self.usetimer = False
            reply_str = f'Disabled All Timers'
        else:
            reply_str = f'Disabled Timer {timer_number}'
        keyboard = [self.create_timer_keyboard()]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)



    # General Section
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
                reply_str += f' All'
            else:
                reply_str += f' Stream {streamid}'
            reply_str += f' at {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
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

    async def run_on_application(self, coro_factory, timeout=30):
        """Await a bot call on the Application's event loop, from wherever we are.

        The local logic runs on its own loop (see run_local_logic) so that arming and the
        alarm relay work with no internet at all, but the bot itself is owned by the
        Application's loop and must only be touched from there. Returns None when Telegram
        is not connected: a missing notification never stops the caller.
        """
        app = self.application
        loop = self.app_loop
        if app is None or loop is None or loop.is_closed() or not app.running:
            mainlogger.debug('Telegram is not connected, skipping notification')
            return None
        try:
            if loop is asyncio.get_running_loop():
                return await asyncio.wait_for(coro_factory(), timeout)
            # run_coroutine_threadsafe hands the coroutine to the Application's loop and
            # gives back a concurrent Future, which wrap_future turns into an awaitable.
            return await asyncio.wait_for(
                asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro_factory(), loop)),
                timeout,
            )
        except Exception as e:
            mainlogger.warning(f'Telegram call failed: {e}')
            return None

    async def send_best_effort(self, chat_ids, text, reply_markup=None):
        """Notify each chat, tolerating a dropped connection.

        Returns the messages that were actually delivered.
        """
        msgs = []
        for chat_id in chat_ids:
            msg = await self.run_on_application(
                lambda cid=chat_id: self.application.bot.sendMessage(
                    text=text, reply_markup=reply_markup, chat_id=cid))
            if msg is not None:
                msgs.append(msg)
        return msgs

    async def edit_best_effort(self, msgs, text, reply_markup=None):
        """Edit previously sent messages, tolerating a dropped connection."""
        for msg in msgs:
            await self.run_on_application(
                lambda m=msg: m.edit_text(text=text, reply_markup=reply_markup))

    async def notify_alarm(self):
        keyboard = [
            [InlineKeyboardButton('Cancel Alarm', callback_data=f'alarm_cancel'),
            InlineKeyboardButton('Confirm Alarm', callback_data=f'alarm_confirm')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        while True:
            if self.streaminfos[0]['alarm'].value == 1:
                timer = 30
                msgs = await self.send_best_effort(
                    Settings.telegram_alarmlist, f'Alarm will trigger in {timer}s', reply_markup)
                await asyncio.sleep(1)
                while (timer > 0) and (self.streaminfos[0]['alarm'].value == 1):
                    timer -= 1
                    await self.edit_best_effort(msgs, f'Alarm will trigger in {timer}s', reply_markup)
                    await asyncio.sleep(1)
                if self.streaminfos[0]['alarm'].value == 1:
                    await self.edit_best_effort(msgs, f'Alarm Triggered Due to Timer Expiration')
                    # Trigger the alarm
                    self.streaminfos[0]['alarm'].value = 0
                    await self.trigger_alarm()
            await asyncio.sleep(0.2)

    @restricted_to_alarmuser
    async def alarm_cancel_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        command = re.match(re.compile('^(alarm_)(.*)$'), query.data).group(2)
        reply_str = 'Error'
        reply_markup = update.effective_message.reply_markup
        if command == 'cancel':
            self.streaminfos[0]['alarm'].value = 0
            reply_str = 'Alarm Cancelled'
        elif command == 'confirm':
            # Trigger the alarm
            self.streaminfos[0]['alarm'].value = 0
            await self.trigger_alarm()
            reply_str = 'Alarm Confirmed'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    async def trigger_alarm(self):
        mainlogger.info('Triggering Alarm')
        try:
            # The relay is on the LAN, but a blocking call with no timeout would still stall
            # the whole event loop if it stops answering.
            await asyncio.to_thread(
                requests.get, f'http://{UserSettings.alarm_relay_ip}/cm?cmnd=Power%20On', timeout=5)
        except Exception:
            mainlogger.exception('Error Triggering Alarm')
            await self.send_best_effort(Settings.telegram_alarmlist, f'Error Triggering Alarm')

    def set_armed(self, armed: int) -> None:
        """Set the master armed flag and persist it, the same way the buttons do.

        The timer path used to skip the db write, so a scheduled arm/disarm was lost on
        restart and the state loaded from the db was already stale before this ran.
        """
        self.streaminfos[0]['armed'].value = armed
        self.dbupdatequeue.put('Update')

    async def notify_when_connected(self, chat_ids, text, wait=300) -> None:
        """Send as soon as Telegram is reachable, without holding up the local logic.

        A startup notification loses the race against the Application: the local loop runs
        long before polling has connected, so sending straight away would just log
        'Telegram is not connected' and drop the message. Arming itself never waits.
        """
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.application is not None and self.app_loop is not None and self.application.running:
                break
            await asyncio.sleep(1)
        await self.send_best_effort(chat_ids, text)

    async def apply_startup_arm_state(self, timerlist) -> None:
        """Put the system into the state the schedule says it should be in.

        The armed state restored from the database is whatever it happened to be when the
        process last died, which after an overnight outage is simply wrong. Rather than
        replaying a week of firings, we ask which timer fired most recently: every earlier
        firing is overwritten by that one anyway.

        Only done once per app start. The flag lives in shared state owned by the parent
        process, so a Telegrambot restart does not silently undo a manual arm/disarm.
        """
        applied = self.streaminfos[0].get('timer_state_applied')
        if applied is not None:
            if applied.value:
                return
            applied.value = 1
        for timer in timerlist:
            if timer.never_fires():
                mainlogger.warning(f'Timer can never fire, check its config: {timer}')
        latest = last_scheduled_action(timerlist)
        if latest is None:
            mainlogger.info('No auto arm/disarm timer has fired yet, keeping the restored state')
            return
        fire_time, timer = latest
        armed = 1 if timer.do_arm else 0
        state_str = 'Armed' if timer.do_arm else 'Disarmed'
        if self.streaminfos[0]['armed'].value == armed:
            mainlogger.info(
                f'Startup state already correct: {timer} last fired at {fire_time}')
            return
        mainlogger.info(f'Startup state corrected to {state_str}: {timer} last fired at {fire_time}')
        self.set_armed(armed)
        # Held as an attribute so the task is not garbage collected mid-flight.
        self.startup_notification = asyncio.create_task(self.notify_when_connected(
            Settings.telegram_notify_arm_disarm_list,
            f'Auto {state_str} on startup (scheduled {fire_time.strftime("%Y-%m-%d %H:%M")})'))

    async def auto_arm_disarm_timer(self):
        timerlist = UserSettings.auto_arm_disarm_list
        check_if_active_time = 1
        if self.usetimer:
            await self.apply_startup_arm_state(timerlist)
        while True:
            if self.usetimer:
                for timer in timerlist:
                    action = timer.check_action()
                    if action is True:
                        mainlogger.info(f'{timer} triggered')
                        self.set_armed(1)
                        await self.send_best_effort(Settings.telegram_notify_arm_disarm_list, f'Auto Armed')
                    elif action is False:
                        mainlogger.info(f'{timer} triggered')
                        self.set_armed(0)
                        await self.send_best_effort(Settings.telegram_notify_arm_disarm_list, f'Auto Disarmed')
                await asyncio.sleep(check_if_active_time)
            else:
                await asyncio.sleep(check_if_active_time)


    async def supervise(self, coro_func, description) -> None:
        """Keep a background loop alive across failures.

        These loops used to be plain fire-and-forget tasks: the first NetworkError killed
        them for good and asyncio only reported it as an unretrieved task exception, so the
        auto-arm timers and the alarm escalation went quiet until the next restart.
        """
        backoff = 1
        while True:
            try:
                await coro_func()
            except asyncio.CancelledError:
                raise
            except Exception:
                mainlogger.exception(f'{description} failed, restarting in {backoff}s')
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
            else:
                return

    async def post_init(self, application: Application) -> None:
        """Publish the Application's loop so the local logic can send through it."""
        self.app_loop = asyncio.get_running_loop()

    def run_local_logic(self) -> None:
        """Event loop for the logic that must not depend on Telegram.

        Arming and firing the alarm relay are local decisions. Running them on the
        Application's loop meant they never started at all when the bot could not reach
        Telegram, because Application.initialize() fails before post_init runs.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(asyncio.gather(
            self.supervise(self.notify_alarm, 'notify_alarm'),
            self.supervise(self.auto_arm_disarm_timer, 'auto_arm_disarm_timer'),
        ))

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        mainlogger.error(f'Exception while handling an update: {context.error}')

    def build_application(self) -> Application:
        """Build a fresh Application. Must be rebuilt per run: run_polling closes the loop."""
        self.application = Application.builder().token(Settings.fractal_token).post_init(
            self.post_init).build()

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
                    CallbackQueryHandler(self.system_management_entry, pattern='^system_management_show$'),
                    CallbackQueryHandler(self.restart_docker, pattern='^restart_docker$'),
                    CallbackQueryHandler(self.timer_entry, pattern='^timer_management_show$'),
                    CallbackQueryHandler(self.enable_timer, pattern='^timer_management_enable_.*$'),
                    CallbackQueryHandler(self.disable_timer, pattern='^timer_management_disable_.*$'),
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

        self.application.add_handler(adminconversation)
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CallbackQueryHandler(self.arm_disarm, pattern='^arm_disarm_.*$'))
        self.application.add_handler(CallbackQueryHandler(self.take_snapshot, pattern='^take_snapshot_.*$'))
        self.application.add_handler(CallbackQueryHandler(self.alarm_cancel_confirm, pattern='^alarm_.*$'))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(MessageHandler(filters.ALL, self.help_command))
        self.application.add_error_handler(self.error_handler)
        return self.application

    def run(self) -> None:
        """Run the bot, retrying until the network comes back.

        Application.initialize() calls get_me(), and start_polling's bootstrap defaults to
        bootstrap_retries=0, so both raise straight out of run_polling if the link is down
        at startup. Combined with the container restart policy and the admin Restart button,
        that used to leave the process dead for good after a restart during an outage.
        """
        # Arming and the alarm relay run on their own loop: they must keep working even
        # while the Application below is stuck retrying a connection it cannot make.
        threading.Thread(target=self.run_local_logic, daemon=True).start()
        backoff = 5
        while True:
            try:
                # run_polling closes the event loop when it returns, so each attempt needs a
                # new one (and a new Application bound to it).
                asyncio.set_event_loop(asyncio.new_event_loop())
                self.build_application().run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    # Retry the bootstrap indefinitely instead of dying on the first failure.
                    bootstrap_retries=-1,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                mainlogger.exception(f'Telegram application stopped, restarting in {backoff}s')
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
            else:
                # run_polling returns normally on SIGINT/SIGTERM: a real shutdown.
                mainlogger.info('Telegrambot shutting down')
                return
            finally:
                # The loop is closed by now: stop the local logic submitting work to it.
                self.app_loop = None


if __name__ == "__main__":
    dict = {
        0:{'armed':mp.Value('i', 1), 'alarm':mp.Value('i', 1),
           'timer_state_applied':mp.Value('i', 0)},
        1:{'armed':mp.Value('i', 1)}
    }
    bot = Telegrambot(dict, mp.Queue())
    bot.run()

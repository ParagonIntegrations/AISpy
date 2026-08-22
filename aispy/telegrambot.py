import asyncio
import copy
import datetime
import io
import json
import math
import os
import re
import subprocess
import time
from functools import wraps
import multiprocessing as mp
import threading
import cv2
import requests

from settings import Settings
from settings_spec import (CATEGORIES, CATEGORY_LABELS, COMMON_CLASSES, SPECS_BY_NAME,
                           STREAM_FIELDS, STREAM_FIELDS_BY_NAME, class_names,
                           format_classes, specs_in)
from settings_store import (ROLE_ADMIN, ROLE_ALARM, ROLE_LABELS, ROLE_NOTIFY_ARM_DISARM,
                            ROLE_USER, get_store)
from utils import mainlogger, optional_setting
import logging
from autoarm import AutoArm, last_scheduled_action
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, MessageHandler, ConversationHandler,
                          ContextTypes, filters)

# The class groups a stream has, keyed by the short token that goes into a callback -
# Telegram gives callback_data 64 bytes, which is not the place for column names.
CLASS_GROUPS = {'sight': 'detection_classes', 'motion': 'motion_classes'}
CLASS_GROUP_KEYS = {column: key for key, column in CLASS_GROUPS.items()}
OTHER_GROUP = {'sight': 'motion', 'motion': 'sight'}
CLASS_GROUP_PATTERN = '|'.join(CLASS_GROUPS)

## Enable logging
# logging.basicConfig(
#     format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
# )
# # set higher logging level for httpx to avoid all GET and POST requests being logged
# logging.getLogger("httpx").setLevel(logging.WARNING)
#
# logger = mainlogger


# Roles as they appear in callback_data. Short keys keep the payload splittable:
# notify_arm_disarm has underscores of its own.
CALLBACK_ROLES = {
    'admin': ROLE_ADMIN,
    'user': ROLE_USER,
    'alarm': ROLE_ALARM,
    'notify': ROLE_NOTIFY_ARM_DISARM,
}
ROLE_CALLBACKS = {role: key for key, role in CALLBACK_ROLES.items()}
ROLE_PATTERN = '|'.join(CALLBACK_ROLES)


def restricted(check):
    """Gate a handler on one of the store's membership checks.

    Membership is looked up per update rather than captured at import: the lists live in
    the database now, so an admin added from another process has to take effect here
    without waiting for a restart.
    """
    def decorate(func):
        @wraps(func)
        async def wrapped(self, update, context, *args, **kwargs):
            user_id = update.effective_user.id
            if not check(get_store(), user_id):
                mainlogger.info(f'Unauthorized access denied for {user_id} on {func.__name__}.')
                return
            return await func(self, update, context, *args, **kwargs)
        return wrapped
    return decorate


restricted_to_admin = restricted(lambda settings, user_id: settings.is_admin(user_id))
restricted_to_user = restricted(lambda settings, user_id: settings.is_user(user_id))
restricted_to_alarmuser = restricted(lambda settings, user_id: settings.is_alarm_user(user_id))

class Telegrambot(mp.Process):

    def __init__(self, streaminfos, dbupdatequeue):
        mainlogger.info(f'Starting Telegrambot')
        super().__init__()
        self.streaminfos: dict = streaminfos
        self.dbupdatequeue = dbupdatequeue
        self.settings = get_store()
        self.application: Application | None = None
        # The Application's event loop, published by post_init while it is connected.
        self.app_loop: asyncio.AbstractEventLoop | None = None
        self.userkeyboard = ReplyKeyboardMarkup([['/start']], is_persistent=True)
        self.adminkeyboard = ReplyKeyboardMarkup([['/start'],['/admin','exit admin']], is_persistent=True)
        # Startup arm/disarm message, waiting for Telegram to come up (see post_init).
        self.startup_notification: asyncio.Task | None = None

    def stream_label(self, streamid) -> str:
        """What a camera is called on a button.

        Read from the database rather than from the forked streaminfos dict, so renaming
        a camera in the panel shows up here without waiting for a restart - unlike every
        other stream field, a name is only ever used to label a button.
        """
        return self.settings.stream_name(streamid)

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
            text += f' {self.stream_label(streamid)}'
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
            text = f'Snapshot for {self.stream_label(streamid)}'
            streambutton = [InlineKeyboardButton(text, callback_data=f'take_snapshot_{streamid}')]
            row = (i+buttons_per_row-1)//buttons_per_row
            keyboard[row] += streambutton
        empty_buttons = (num_rows * buttons_per_row - (num_buttons + buttons_per_row - 1))
        empties = [InlineKeyboardButton('Empty Stream', callback_data='None') for x in range(empty_buttons)]
        keyboard[-1] += empties
        return keyboard

    # -- panel plumbing ------------------------------------------------------

    @staticmethod
    async def show(query, text, reply_markup=None) -> None:
        """Replace the message a button was pressed on.

        Telegram rejects an edit that changes nothing, which a menu full of Back buttons
        makes easy to hit: leave a submenu and come straight back and the text is
        identical. That is not a failure anyone needs to hear about.
        """
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        except BadRequest as e:
            if 'not modified' not in str(e).lower():
                raise

    @staticmethod
    def joined(*parts) -> str:
        return '\n\n'.join(part for part in parts if part)

    @staticmethod
    def shorten(text, limit=32) -> str:
        """Keep a value readable on a button. URLs are the reason this exists."""
        text = str(text)
        return text if len(text) <= limit else f'{text[:limit - 1]}\u2026'

    def restart_note(self) -> str:
        """Said out loud wherever stream configuration is on screen.

        A stream edit only reaches the running processes when they are forked again, so
        an admin who changes a URL and walks away has changed nothing yet. The flag is
        in the database and cleared by FractalApp on the way up, so it survives the bot
        being restarted on its own and disappears exactly when the change takes effect.
        """
        return ('\u26a0 Stream changes are waiting for a restart.'
                if self.settings.streams_dirty() else '')

    def restart_rows(self) -> list:
        if not self.settings.streams_dirty():
            return []
        return [[InlineKeyboardButton('\u26a0 Restart to apply stream changes',
                                      callback_data='restart_docker')]]

    def admin_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton('User Management', callback_data=f'user_management_show')],
            [InlineKeyboardButton('Stream Management', callback_data=f'stream_management_show')],
            [InlineKeyboardButton('System Settings', callback_data=f'system_management_show')],
        ]
        return InlineKeyboardMarkup(keyboard + self.restart_rows())

    @restricted_to_admin
    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        await update.effective_message.reply_text(
            self.joined('Choose an action:', self.restart_note()),
            reply_markup=self.admin_keyboard())
        return 'inline_keyboard'

    @restricted_to_admin
    async def admin_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """The same menu, for the Back buttons that have somewhere to go back to."""
        query = update.callback_query
        await query.answer()
        await self.show(query, self.joined('Choose an action:', self.restart_note()),
                        self.admin_keyboard())

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
                InlineKeyboardButton(f'Add {ROLE_LABELS[role]}',
                                     callback_data=f'user_management_add_{key}'),
                InlineKeyboardButton(f'Remove {ROLE_LABELS[role]}',
                                     callback_data=f'user_management_remove_{key}'),
            ]
            for key, role in CALLBACK_ROLES.items()
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text='Choose an option', reply_markup=reply_markup)

    def member_list_str(self, role) -> str:
        """The current holders of a role, plus the superadmins that outrank them."""
        lines = [f'Current {ROLE_LABELS[role]}s are', '******']
        if role == ROLE_ADMIN:
            lines += [f'{user_id} (superadmin, set in settings.py)'
                      for user_id in self.settings.superadmins()]
        lines += [str(user_id) for user_id in self.settings.members(role)]
        lines.append('******')
        return '\n'.join(lines)

    @restricted_to_admin
    async def user_management_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Add someone to a role, one typed id at a time.

        One handler for every role: the role is carried in the callback that opened text
        mode and kept in user_data for as long as the conversation stays in that state.
        """
        # Callback origin: entering text mode.
        if update.message is None:
            query = update.callback_query
            await query.answer()
            key = re.match(f'^user_management_add_({ROLE_PATTERN})$', query.data).group(1)
            context.user_data['role'] = CALLBACK_ROLES[key]
            await update.effective_message.edit_text('Entering Text Mode')
        # Message origin: an id to add.
        else:
            role = context.user_data.get('role')
            if role is None:
                await update.effective_message.reply_text('Start again from /admin')
                return ConversationHandler.END
            try:
                self.settings.add_member(role, int(update.message.text))
            except Exception as e:
                await update.effective_message.reply_text(text=f'{e} \n Please try again')
                return 'add_member_text_input'
            mainlogger.info(f'{update.effective_user.id} added {update.message.text} as {role}')
        reply_str = (f'{self.member_list_str(context.user_data["role"])}\n'
                     f'Type an additional id to add')
        await update.effective_message.reply_text(text=reply_str)
        return 'add_member_text_input'

    @restricted_to_admin
    async def user_management_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List a role's members as remove buttons, and act on one when it is pressed."""
        query = update.callback_query
        await query.answer()
        match = re.match(f'^user_management_(remove|del)_({ROLE_PATTERN})(?:_(-?\\d+))?$', query.data)
        role = CALLBACK_ROLES[match.group(2)]
        reply_str = f'Choose a {ROLE_LABELS[role]} to remove'
        if match.group(1) == 'del':
            user_id = int(match.group(3))
            # Emptying the admin list with no superadmin configured locks everyone out of
            # the panel, and the only way back would be hand-edited SQL.
            if (role == ROLE_ADMIN and not self.settings.superadmins()
                    and self.settings.members(ROLE_ADMIN) == [user_id]):
                reply_str = ('Cannot remove the last admin while no superadmin is set in '
                             'settings.py')
            else:
                self.settings.remove_member(role, user_id)
                mainlogger.info(f'{update.effective_user.id} removed {user_id} from {role}')
        keyboard = [
            [InlineKeyboardButton(f'Remove {user_id}',
                                  callback_data=f'user_management_del_{ROLE_CALLBACKS[role]}_{user_id}')]
            for user_id in self.settings.members(role)
        ]
        keyboard += [[InlineKeyboardButton('Back to User Management',
                                           callback_data='user_management_show')]]
        await query.edit_message_text(text=reply_str, reply_markup=InlineKeyboardMarkup(keyboard))

    # Stream Management Section
    #
    # Every field the editor offers comes from settings_spec.STREAM_FIELDS, so a new
    # column is an entry in that table rather than a handler here. Two columns are
    # deliberately not in it: `armed` belongs to the arm/disarm buttons, and the detect
    # area is only ever written when a stream is created - there is no way to draw a
    # polygon on a phone keyboard, and guessing would quietly destroy a hand-drawn one.

    # How long ffprobe gets to answer before we give up and use a placeholder.
    probe_timeout = 30

    @staticmethod
    def stream_title(row) -> str:
        name = row['name']
        return f'{name} (stream {row["streamid"]})' if name else f'Stream {row["streamid"]}'

    def stream_menu_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = [[InlineKeyboardButton(self.stream_title(row),
                                          callback_data=f'stream_show_{row["streamid"]}')]
                    for row in self.settings.stream_rows()]
        keyboard.append([InlineKeyboardButton('Add Stream', callback_data='stream_add')])
        keyboard += self.restart_rows()
        keyboard.append([InlineKeyboardButton('Back to Admin', callback_data='admin_menu_show')])
        return InlineKeyboardMarkup(keyboard)

    def stream_menu(self, prefix='') -> tuple:
        streams = self.settings.stream_rows()
        heading = 'Choose a stream' if streams else 'No streams configured yet'
        return (self.joined(prefix, heading, self.restart_note()),
                self.stream_menu_keyboard())

    @restricted_to_admin
    async def stream_management_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        await self.show(query, *self.stream_menu())

    def field_display(self, field, value) -> str:
        """A field's value as the panel shows it, names rather than class numbers."""
        if field.name in CLASS_GROUP_KEYS:
            return format_classes(value)
        return field.format(value)

    def stream_editor_keyboard(self, row) -> InlineKeyboardMarkup:
        streamid = row['streamid']
        keyboard = []
        for field in STREAM_FIELDS:
            if field.kind == 'bool':
                callback = f'stream_toggle_{streamid}_{field.name}'
            elif field.name in CLASS_GROUP_KEYS:
                callback = f'stream_classes_{streamid}_{CLASS_GROUP_KEYS[field.name]}'
            else:
                callback = f'stream_edit_{streamid}_{field.name}'
            keyboard.append([InlineKeyboardButton(
                f'{field.label}: {self.shorten(self.field_display(field, row[field.name]))}',
                callback_data=callback)])
        keyboard.append([InlineKeyboardButton('Delete Stream',
                                              callback_data=f'stream_remove_{streamid}')])
        keyboard += self.restart_rows()
        keyboard.append([InlineKeyboardButton('Back to Streams',
                                              callback_data='stream_management_show')])
        return InlineKeyboardMarkup(keyboard)

    def stream_editor(self, streamid, prefix='') -> tuple:
        """(text, keyboard) for one stream, falling back to the list if it is gone."""
        row = self.settings.stream_row(streamid)
        if row is None:
            return self.stream_menu(self.joined(prefix, 'That stream no longer exists'))
        return (self.joined(prefix, self.stream_title(row), self.restart_note()),
                self.stream_editor_keyboard(row))

    @restricted_to_admin
    async def stream_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        streamid = int(re.match(r'^stream_show_(\d+)$', query.data).group(1))
        await self.show(query, *self.stream_editor(streamid))

    # -- adding --------------------------------------------------------------

    async def probe_dimensions(self, url) -> tuple:
        """Ask ffprobe how big the picture is. Returns (dimensions, complaint).

        A camera that is merely unplugged this minute is still worth configuring, so a
        probe that fails is a warning and a placeholder rather than a refusal. Run in a
        thread: the admin's message is being handled on the Application's event loop, and
        an unreachable camera takes the full timeout to say so.
        """
        ffprobe = optional_setting('ffprobe_path', 'ffprobe')
        command = [ffprobe, '-v', 'error']
        if str(url).lower().startswith('rtsp://'):
            # Matching the recorder: UDP loses packets on anything but a quiet LAN.
            command += ['-rtsp_transport', 'tcp']
        command += ['-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                    '-of', 'csv=s=x:p=0', str(url)]
        try:
            result = await asyncio.to_thread(
                subprocess.run, command, capture_output=True, timeout=self.probe_timeout)
            first_line = result.stdout.decode(errors='replace').strip().splitlines()[0]
            width, height = (int(part) for part in first_line.split('x')[:2])
            return (width, height), ''
        except Exception:
            mainlogger.warning(f'Could not probe the dimensions of {url}')
            width, height = STREAM_FIELDS_BY_NAME['dimensions'].default
            return None, (f'Could not read the picture size from that URL, so Dimensions '
                          f'are set to {width}x{height}. Check them before restarting.')

    @restricted_to_admin
    async def stream_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        query = update.callback_query
        await query.answer()
        context.user_data['stream_id'] = None
        context.user_data['stream_field'] = 'url'
        await self.show(query, self.joined(
            'Paste the URL of the stream to record from.',
            'ffprobe is asked for its dimensions; everything else starts at its default '
            'and can be changed on the next screen.'))
        return 'stream_text_input'

    async def create_stream(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url) -> str:
        await update.effective_message.reply_text('Probing the stream, one moment')
        dimensions, complaint = await self.probe_dimensions(url)
        try:
            streamid = self.settings.add_stream(url, dimensions=dimensions)
        except Exception as e:
            await update.effective_message.reply_text(f'{e}\n\nPaste a URL to try again')
            return 'stream_text_input'
        mainlogger.info(f'{update.effective_user.id} added stream {streamid} on {url}')
        context.user_data['stream_id'] = streamid
        context.user_data['stream_field'] = None
        text, reply_markup = self.stream_editor(
            streamid, self.joined(f'Stream {streamid} created.', complaint))
        await update.effective_message.reply_text(text, reply_markup=reply_markup)
        return 'inline_keyboard'

    # -- editing -------------------------------------------------------------

    def stream_field_prompt(self, field, row=None) -> str:
        lines = [field.label, field.description]
        if row is not None:
            lines.append(f'Current: {self.field_display(field, row[field.name])}')
        if field.name in CLASS_GROUP_KEYS:
            lines.append('Type class names or numbers, separated by commas. '
                         'Send - to clear the list.')
            lines.append('A class listed here is taken off the other list: each one '
                         'either counts on sight or only while moving, not both.')
        elif field.optional:
            lines.append(f'Send - to clear it back to {field.unset_label}.')
        else:
            lines.append('Type a new value')
        return self.joined(*lines)

    @restricted_to_admin
    async def stream_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        query = update.callback_query
        await query.answer()
        streamid, name = re.match(r'^stream_edit_(\d+)_(\w+)$', query.data).groups()
        streamid = int(streamid)
        row = self.settings.stream_row(streamid)
        if row is None:
            await self.show(query, *self.stream_menu('That stream no longer exists'))
            return 'inline_keyboard'
        context.user_data['stream_id'] = streamid
        context.user_data['stream_field'] = name
        await self.show(query, self.stream_field_prompt(STREAM_FIELDS_BY_NAME[name], row))
        return 'stream_text_input'

    @restricted_to_admin
    async def stream_edit_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        name = context.user_data.get('stream_field')
        if name is None:
            await update.effective_message.reply_text('Start again from /admin')
            return ConversationHandler.END
        field = STREAM_FIELDS_BY_NAME[name]
        # An empty Telegram message is not a thing, so a lone dash is how an admin says
        # 'nothing' - unset for an optional field, every class for the class list.
        text = update.message.text
        text = '' if text.strip() == '-' else text
        streamid = context.user_data.get('stream_id')
        try:
            value = field.parse(text)
            if streamid is not None:
                self.settings.set_stream_field(streamid, name, value)
        except Exception as e:
            row = None if streamid is None else self.settings.stream_row(streamid)
            await update.effective_message.reply_text(
                self.joined(str(e), self.stream_field_prompt(field, row)))
            return 'stream_text_input'
        if streamid is None:
            return await self.create_stream(update, context, value)
        mainlogger.info(f'{update.effective_user.id} set stream {streamid} {name} to '
                        f'{self.field_display(field, value)}')
        context.user_data['stream_field'] = None
        text, reply_markup = self.stream_editor(
            streamid, f'{field.label} is now {self.field_display(field, value)}')
        await update.effective_message.reply_text(text, reply_markup=reply_markup)
        return 'inline_keyboard'

    @restricted_to_admin
    async def stream_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        streamid, name = re.match(r'^stream_toggle_(\d+)_(\w+)$', query.data).groups()
        streamid = int(streamid)
        field = STREAM_FIELDS_BY_NAME[name]
        row = self.settings.stream_row(streamid)
        if row is None:
            await self.show(query, *self.stream_editor(streamid))
            return
        value = self.settings.set_stream_field(streamid, name, not row[name])
        mainlogger.info(f'{update.effective_user.id} set stream {streamid} {name} to '
                        f'{field.format(value)}')
        await self.show(query, *self.stream_editor(
            streamid, f'{field.label} is now {field.format(value)}'))

    # -- detection classes ---------------------------------------------------

    def stream_classes_keyboard(self, streamid, group, row) -> InlineKeyboardMarkup:
        """Toggles for what a security camera is usually pointed at.

        The rest of coco.yaml is still reachable, by typing it: eighty paged buttons to
        find 'zebra' is not worth the screens it would take.

        Every class shows which group it is in, not just whether it is in this one, so
        an admin can see that ticking 'car' here is what took it off the other list.
        """
        names = class_names()
        selected = row[CLASS_GROUPS[group]]
        elsewhere = row[CLASS_GROUPS[OTHER_GROUP[group]]]
        keyboard = []
        for index in range(0, len(COMMON_CLASSES), 2):
            keyboard.append([
                InlineKeyboardButton(
                    f'{"✓ " if class_id in selected else "· " if class_id in elsewhere else ""}'
                    f'{names.get(class_id, class_id)}',
                    callback_data=f'stream_class_{streamid}_{group}_{class_id}')
                for class_id in COMMON_CLASSES[index:index + 2]])
        keyboard.append([InlineKeyboardButton(
            'Type other classes',
            callback_data=f'stream_edit_{streamid}_{CLASS_GROUPS[group]}')])
        keyboard.append([InlineKeyboardButton('Done', callback_data=f'stream_show_{streamid}')])
        return InlineKeyboardMarkup(keyboard)

    def stream_classes(self, streamid, group, prefix='') -> tuple:
        row = self.settings.stream_row(streamid)
        if row is None:
            return self.stream_menu(self.joined(prefix, 'That stream no longer exists'))
        field = STREAM_FIELDS_BY_NAME[CLASS_GROUPS[group]]
        other = STREAM_FIELDS_BY_NAME[CLASS_GROUPS[OTHER_GROUP[group]]]
        return (self.joined(prefix, self.stream_title(row), field.description,
                            f'{field.label}: {format_classes(row[field.name])}',
                            f'{other.label} (·): {format_classes(row[other.name])}'),
                self.stream_classes_keyboard(streamid, group, row))

    @restricted_to_admin
    async def stream_classes_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        streamid, group = re.match(r'^stream_classes_(\d+)_(\w+)$', query.data).groups()
        await self.show(query, *self.stream_classes(int(streamid), group))

    @restricted_to_admin
    async def stream_class_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        streamid, group, class_id = re.match(
            r'^stream_class_(\d+)_(\w+)_(\d+)$', query.data).groups()
        streamid, class_id = int(streamid), int(class_id)
        name = CLASS_GROUPS[group]
        row = self.settings.stream_row(streamid)
        if row is None:
            await self.show(query, *self.stream_classes(streamid, group))
            return
        selected = list(row[name])
        if class_id in selected:
            selected.remove(class_id)
        else:
            # Taking it off the other group is the store's job, so a class cannot end up
            # counting on sight and only-while-moving at the same time.
            selected.append(class_id)
        self.settings.set_stream_field(streamid, name, sorted(selected))
        mainlogger.info(f'{update.effective_user.id} set stream {streamid} {name} to '
                        f'{format_classes(selected)}')
        await self.show(query, *self.stream_classes(streamid, group))

    # -- deleting ------------------------------------------------------------

    @restricted_to_admin
    async def stream_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        streamid = int(re.match(r'^stream_remove_(\d+)$', query.data).group(1))
        row = self.settings.stream_row(streamid)
        if row is None:
            await self.show(query, *self.stream_menu('That stream no longer exists'))
            return
        keyboard = [
            [InlineKeyboardButton('Yes, delete it', callback_data=f'stream_delete_{streamid}')],
            [InlineKeyboardButton('Cancel', callback_data=f'stream_show_{streamid}')],
        ]
        await self.show(query, self.joined(
            f'Delete {self.stream_title(row)}?', f'URL: {row["url"]}',
            'Clips and segments already on disk are kept.'), InlineKeyboardMarkup(keyboard))

    @restricted_to_admin
    async def stream_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        streamid = int(re.match(r'^stream_delete_(\d+)$', query.data).group(1))
        row = self.settings.stream_row(streamid)
        if row is None:
            await self.show(query, *self.stream_menu('That stream no longer exists'))
            return
        title = self.stream_title(row)
        self.settings.remove_stream(streamid)
        mainlogger.info(f'{update.effective_user.id} deleted stream {streamid}')
        await self.show(query, *self.stream_menu(f'Deleted {title}'))

    # System Management Section
    @restricted_to_admin
    async def system_management_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        keyboard = [
                [InlineKeyboardButton('Restart', callback_data=f'restart_docker'),],
                [InlineKeyboardButton('Timers', callback_data=f'timer_management_show'),],
                [InlineKeyboardButton('Tunables', callback_data=f'settings_show'),],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        reply_str = 'Choose an option'
        await query.edit_message_text(text=reply_str, reply_markup=reply_markup)

    # Tunables Section
    #
    # Generated from settings_spec.SPECS rather than hand-written per setting: adding a
    # tunable is an entry in that table, not a new handler here.
    @restricted_to_admin
    async def settings_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        keyboard = [[InlineKeyboardButton(CATEGORY_LABELS[category],
                                          callback_data=f'settings_category_{category}')]
                    for category in CATEGORIES]
        keyboard += [[InlineKeyboardButton('Back to System Settings',
                                           callback_data='system_management_show')]]
        await query.edit_message_text(text='Choose a group',
                                      reply_markup=InlineKeyboardMarkup(keyboard))

    def settings_category_keyboard(self, category) -> InlineKeyboardMarkup:
        keyboard = []
        for spec in specs_in(category):
            value = self.settings.get(spec.name)
            # A boolean is one button: asking someone to type 'off' would be silly.
            action = 'toggle' if spec.kind == 'bool' else 'edit'
            keyboard.append([InlineKeyboardButton(
                f'{spec.label}: {spec.format(value)}',
                callback_data=f'settings_{action}_{spec.name}')])
        keyboard += [[InlineKeyboardButton('Back to Tunables', callback_data='settings_show')]]
        return InlineKeyboardMarkup(keyboard)

    @restricted_to_admin
    async def settings_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        category = re.match('^settings_category_(.*)$', query.data).group(1)
        context.user_data['settings_category'] = category
        await query.edit_message_text(
            text=f'{CATEGORY_LABELS[category]} settings, choose one to change',
            reply_markup=self.settings_category_keyboard(category))

    @restricted_to_admin
    async def settings_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        spec = SPECS_BY_NAME[re.match('^settings_toggle_(.*)$', query.data).group(1)]
        value = self.settings.set(spec.name, not self.settings.get(spec.name))
        mainlogger.info(f'{update.effective_user.id} set {spec.name} to {spec.format(value)}')
        await query.edit_message_text(
            text=f'{spec.label} is now {spec.format(value)}',
            reply_markup=self.settings_category_keyboard(spec.category))

    def settings_prompt(self, spec) -> str:
        lines = [spec.label, spec.description,
                 f'Current: {spec.format(self.settings.get(spec.name))}',
                 f'Default: {spec.format(spec.default)}']
        if spec.minimum is not None or spec.maximum is not None:
            lines.append(f'Allowed: {spec.minimum} to {spec.maximum}')
        units = ' in seconds' if spec.kind == 'seconds' else ''
        lines.append(f'Type a new value{units}')
        return '\n'.join(line for line in lines if line)

    @restricted_to_admin
    async def settings_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        query = update.callback_query
        await query.answer()
        spec = SPECS_BY_NAME[re.match('^settings_edit_(.*)$', query.data).group(1)]
        context.user_data['setting'] = spec.name
        await query.edit_message_text(text=self.settings_prompt(spec))
        return 'setting_text_input'

    @restricted_to_admin
    async def settings_edit_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        name = context.user_data.get('setting')
        if name is None:
            await update.effective_message.reply_text('Start again from /admin')
            return ConversationHandler.END
        spec = SPECS_BY_NAME[name]
        try:
            value = self.settings.set_from_text(name, update.message.text)
        except Exception as e:
            await update.effective_message.reply_text(f'{e}\n\n{self.settings_prompt(spec)}')
            return 'setting_text_input'
        mainlogger.info(f'{update.effective_user.id} set {name} to {spec.format(value)}')
        await update.effective_message.reply_text(
            f'{spec.label} is now {spec.format(value)}',
            reply_markup=self.settings_category_keyboard(spec.category))
        return 'inline_keyboard'

    @restricted_to_admin
    async def restart_docker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        reply_str = 'Restarting'
        await query.edit_message_text(text=reply_str)
        os.kill(1, 9)

    # Timer Management Section
    #
    # Unlike streams, these are live: auto_arm_disarm_timer re-reads the schedule every
    # pass, so an added or edited timer takes effect without a restart.

    DAY_LETTERS = 'MTWTFSS'

    def create_timer_keyboard(self) -> list:
        """The master switch, then one row per timer, then the two Add buttons.

        The per-timer buttons used to be unreachable: timers came from a Python list in
        settings.py with nothing to address them by. They have row ids now, so the label
        opens that one timer and the state next to it flips it without leaving the list.
        """
        if self.settings.get('timers_enabled'):
            keyboard = [[InlineKeyboardButton('Disable All Timers',
                                              callback_data='timer_management_disable_0')]]
        else:
            keyboard = [[InlineKeyboardButton('Enable All Timers',
                                              callback_data='timer_management_enable_0')]]
        for row in self.settings.timer_rows():
            action = 'disable' if row['enabled'] else 'enable'
            state = 'on' if row['enabled'] else 'off'
            keyboard.append([
                InlineKeyboardButton(self.timer_str(row),
                                     callback_data=f'timer_show_{row["id"]}'),
                InlineKeyboardButton(f'[{state}]',
                                     callback_data=f'timer_management_{action}_{row["id"]}'),
            ])
        keyboard.append([
            InlineKeyboardButton('Add Arm timer', callback_data='timer_add_arm'),
            InlineKeyboardButton('Add Disarm timer', callback_data='timer_add_disarm'),
        ])
        keyboard.append([InlineKeyboardButton('Back to System Settings',
                                              callback_data='system_management_show')])
        return keyboard

    @staticmethod
    def timer_str(row) -> str:
        arm_str = 'Arm' if row['do_arm'] else 'Disarm'
        days = ''.join(Telegrambot.DAY_LETTERS[day]
                       for day in sorted(json.loads(row['active_days'])))
        every = '' if row['repeat_every_days'] == 1 else f'/{row["repeat_every_days"]}d'
        return f'{arm_str} {row["hour"]:02d}:{row["minute"]:02d} {days}{every}'

    def timer_menu(self, prefix='') -> tuple:
        heading = ('Choose a timer, or add one' if self.settings.timer_rows()
                   else 'No timers configured yet')
        if not self.settings.get('timers_enabled'):
            heading = self.joined(heading, 'The master switch is off, so none of them fire.')
        return self.joined(prefix, heading), InlineKeyboardMarkup(self.create_timer_keyboard())

    @restricted_to_admin
    async def timer_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        await self.show(query, *self.timer_menu())

    @restricted_to_admin
    async def set_timer_enabled(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle the master switch (id 0) or a single timer, from the list.

        Both live in the database now: the master switch used to be an attribute on this
        process, so 'Disable All Timers' quietly un-disabled itself whenever the
        supervisor restarted the bot.
        """
        query = update.callback_query
        await query.answer()
        action, timer_id = re.match(
            r'^timer_management_(enable|disable)_(\d+)$', query.data).groups()
        enabled = action == 'enable'
        timer_id = int(timer_id)
        if timer_id == 0:
            self.settings.set('timers_enabled', enabled)
            reply_str = f'{action.capitalize()}d All Timers'
        else:
            row = self.settings.timer_row(timer_id)
            if row is None:
                reply_str = 'That timer no longer exists'
            else:
                self.settings.set_timer_enabled(timer_id, enabled)
                reply_str = f'{action.capitalize()}d {self.timer_str(row)}'
        mainlogger.info(f'{update.effective_user.id}: {reply_str}')
        await self.show(query, *self.timer_menu(reply_str))

    # -- one timer -----------------------------------------------------------

    def timer_next_str(self, row) -> str:
        """When this timer fires next, or why it never will.

        Worth the arithmetic: a weekday list and an every-N-days repeat that never line
        up produce a timer that looks perfectly reasonable in the list and does nothing
        for the rest of its life.
        """
        timer = AutoArm(hour=row['hour'], minute=row['minute'],
                        repeat_every_days=row['repeat_every_days'],
                        active_days=json.loads(row['active_days']),
                        do_arm=bool(row['do_arm']))
        if timer.next_time is None:
            return ('⚠ This timer can never fire: no day matches both its weekdays '
                    'and its repeat.')
        if not row['enabled']:
            return f'Disabled. Would next fire {timer.next_time.strftime("%a %d %b at %H:%M")}'
        if not self.settings.get('timers_enabled'):
            return (f'The master switch is off. Would next fire '
                    f'{timer.next_time.strftime("%a %d %b at %H:%M")}')
        return f'Next: {timer.next_time.strftime("%a %d %b at %H:%M")}'

    def timer_editor_keyboard(self, row) -> InlineKeyboardMarkup:
        timer_id = row['id']
        active_days = json.loads(row['active_days'])
        keyboard = [
            [InlineKeyboardButton(f'Time: {row["hour"]:02d}:{row["minute"]:02d}',
                                  callback_data=f'timer_edit_{timer_id}_time')],
            [InlineKeyboardButton(f'Action: {"Arm" if row["do_arm"] else "Disarm"}',
                                  callback_data=f'timer_action_{timer_id}')],
            [InlineKeyboardButton(f'Repeat: every {row["repeat_every_days"]}d',
                                  callback_data=f'timer_edit_{timer_id}_repeat')],
            [InlineKeyboardButton(f'{"✓" if day in active_days else ""}{self.DAY_LETTERS[day]}',
                                  callback_data=f'timer_day_{timer_id}_{day}')
             for day in range(7)],
            [InlineKeyboardButton('Disable' if row['enabled'] else 'Enable',
                                  callback_data=f'timer_toggle_{timer_id}'),
             InlineKeyboardButton('Delete', callback_data=f'timer_remove_{timer_id}')],
            [InlineKeyboardButton('Back to Timers', callback_data='timer_management_show')],
        ]
        return InlineKeyboardMarkup(keyboard)

    def timer_editor(self, timer_id, prefix='') -> tuple:
        """(text, keyboard) for one timer, falling back to the list if it is gone."""
        row = self.settings.timer_row(timer_id)
        if row is None:
            return self.timer_menu(self.joined(prefix, 'That timer no longer exists'))
        return (self.joined(prefix, self.timer_str(row), self.timer_next_str(row)),
                self.timer_editor_keyboard(row))

    @restricted_to_admin
    async def timer_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        timer_id = int(re.match(r'^timer_show_(\d+)$', query.data).group(1))
        await self.show(query, *self.timer_editor(timer_id))

    @restricted_to_admin
    async def timer_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable or disable from inside the timer's own screen, and stay there."""
        query = update.callback_query
        await query.answer()
        timer_id = int(re.match(r'^timer_toggle_(\d+)$', query.data).group(1))
        row = self.settings.timer_row(timer_id)
        if row is None:
            await self.show(query, *self.timer_editor(timer_id))
            return
        enabled = not row['enabled']
        self.settings.set_timer_enabled(timer_id, enabled)
        mainlogger.info(f'{update.effective_user.id} '
                        f'{"enabled" if enabled else "disabled"} {self.timer_str(row)}')
        await self.show(query, *self.timer_editor(
            timer_id, 'Enabled' if enabled else 'Disabled'))

    @restricted_to_admin
    async def timer_action_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        timer_id = int(re.match(r'^timer_action_(\d+)$', query.data).group(1))
        row = self.settings.timer_row(timer_id)
        if row is None:
            await self.show(query, *self.timer_editor(timer_id))
            return
        do_arm = not row['do_arm']
        self.settings.update_timer(timer_id, do_arm=do_arm)
        mainlogger.info(f'{update.effective_user.id} made timer {timer_id} '
                        f'{"arm" if do_arm else "disarm"}')
        await self.show(query, *self.timer_editor(
            timer_id, f'Now {"an arm" if do_arm else "a disarm"} timer'))

    @restricted_to_admin
    async def timer_day_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        timer_id, day = re.match(r'^timer_day_(\d+)_(\d+)$', query.data).groups()
        timer_id, day = int(timer_id), int(day)
        row = self.settings.timer_row(timer_id)
        if row is None:
            await self.show(query, *self.timer_editor(timer_id))
            return
        active_days = json.loads(row['active_days'])
        if day in active_days:
            active_days.remove(day)
        else:
            active_days.append(day)
        try:
            self.settings.update_timer(timer_id, active_days=active_days)
        except ValueError as e:
            # Emptying the list would be a timer that fires on no day at all.
            await self.show(query, *self.timer_editor(timer_id, str(e)))
            return
        await self.show(query, *self.timer_editor(timer_id))

    # -- adding --------------------------------------------------------------

    @restricted_to_admin
    async def timer_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Ask for the time; the timer itself is created once we have one.

        It starts on every day of the week, which is both the common case and the only
        answer that cannot be wrong in a way that silently does nothing.
        """
        query = update.callback_query
        await query.answer()
        do_arm = re.match(r'^timer_add_(arm|disarm)$', query.data).group(1) == 'arm'
        context.user_data['timer_id'] = None
        context.user_data['timer_field'] = 'time'
        context.user_data['timer_do_arm'] = do_arm
        await self.show(query, self.joined(
            f'New {"arm" if do_arm else "disarm"} timer.',
            'Type the time as HH:MM'))
        return 'timer_text_input'

    def parse_hhmm(self, text) -> tuple:
        match = re.match(r'^(\d{1,2})\s*[:.h]?\s*(\d{2})?$', text.strip())
        if match is None:
            raise ValueError('Type the time as HH:MM, like 22:30')
        return self.settings.check_time(match.group(1), match.group(2) or 0)

    @staticmethod
    def timer_field_prompt(field) -> str:
        if field == 'time':
            return 'Type the time as HH:MM'
        return ('Type how many days apart this timer repeats. 1 is every day it is '
                'allowed to fire, 7 makes it the same weekday every week.')

    @restricted_to_admin
    async def timer_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        query = update.callback_query
        await query.answer()
        timer_id, field = re.match(r'^timer_edit_(\d+)_(time|repeat)$', query.data).groups()
        timer_id = int(timer_id)
        if self.settings.timer_row(timer_id) is None:
            await self.show(query, *self.timer_menu('That timer no longer exists'))
            return 'inline_keyboard'
        context.user_data['timer_id'] = timer_id
        context.user_data['timer_field'] = field
        await self.show(query, self.timer_field_prompt(field))
        return 'timer_text_input'

    @restricted_to_admin
    async def timer_edit_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        field = context.user_data.get('timer_field')
        if field is None:
            await update.effective_message.reply_text('Start again from /admin')
            return ConversationHandler.END
        timer_id = context.user_data.get('timer_id')
        prefix = ''
        try:
            if field == 'time':
                hour, minute = self.parse_hhmm(update.message.text)
                if timer_id is None:
                    timer_id = self.settings.add_timer(
                        hour=hour, minute=minute,
                        do_arm=context.user_data.get('timer_do_arm', True))
                    context.user_data['timer_id'] = timer_id
                    prefix = 'Created, on every day of the week. Turn off the days it should skip.'
                else:
                    self.settings.update_timer(timer_id, hour=hour, minute=minute)
                    prefix = f'Time is now {hour:02d}:{minute:02d}'
            else:
                repeat = int(update.message.text.strip())
                self.settings.update_timer(timer_id, repeat_every_days=repeat)
                prefix = f'Repeating every {max(1, repeat)}d'
        except Exception as e:
            await update.effective_message.reply_text(
                self.joined(str(e), self.timer_field_prompt(field)))
            return 'timer_text_input'
        mainlogger.info(f'{update.effective_user.id}: timer {timer_id} - {prefix}')
        context.user_data['timer_field'] = None
        text, reply_markup = self.timer_editor(timer_id, prefix)
        await update.effective_message.reply_text(text, reply_markup=reply_markup)
        return 'inline_keyboard'

    # -- deleting ------------------------------------------------------------

    @restricted_to_admin
    async def timer_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        timer_id = int(re.match(r'^timer_remove_(\d+)$', query.data).group(1))
        row = self.settings.timer_row(timer_id)
        if row is None:
            await self.show(query, *self.timer_menu('That timer no longer exists'))
            return
        keyboard = [
            [InlineKeyboardButton('Yes, delete it', callback_data=f'timer_delete_{timer_id}')],
            [InlineKeyboardButton('Cancel', callback_data=f'timer_show_{timer_id}')],
        ]
        await self.show(query, f'Delete {self.timer_str(row)}?',
                        InlineKeyboardMarkup(keyboard))

    @restricted_to_admin
    async def timer_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        timer_id = int(re.match(r'^timer_delete_(\d+)$', query.data).group(1))
        row = self.settings.timer_row(timer_id)
        if row is None:
            await self.show(query, *self.timer_menu('That timer no longer exists'))
            return
        description = self.timer_str(row)
        self.settings.remove_timer(timer_id)
        mainlogger.info(f'{update.effective_user.id} deleted {description}')
        await self.show(query, *self.timer_menu(f'Deleted {description}'))

    # General Section
    @restricted_to_user
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        keyboard = [
            [InlineKeyboardButton('Arm/Disarm', callback_data=f'arm_disarm_show')],
            [InlineKeyboardButton('Snapshots', callback_data=f'take_snapshot_show')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if self.settings.is_admin(update.effective_user.id):
            reply_keyboard_markup = self.adminkeyboard
        elif self.settings.is_user(update.effective_user.id):
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
            reply_str += f' {self.stream_label(streamid)}'
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
                await update.effective_message.reply_photo(io_buf, self.stream_label(stream))
            await self.start_command(update, context)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Displays info on how to use the bot."""
        non_user_reply_str = 'Please contact an admin to get access to this bot'
        user_reply_str = 'Use /start to use this bot'
        admin_reply_str = user_reply_str + ''
        if self.settings.is_admin(update.effective_user.id):
            reply_str = admin_reply_str
            reply_markup = self.adminkeyboard
        elif self.settings.is_user(update.effective_user.id):
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
                timer = int(self.settings.get('alarm_countdown').total_seconds())
                msgs = await self.send_best_effort(
                    self.settings.alarm_chat_ids(), f'Alarm will trigger in {timer}s', reply_markup)
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
                requests.get,
                f'http://{self.settings.get("alarm_relay_ip")}/cm?cmnd=Power%20On', timeout=5)
        except Exception:
            mainlogger.exception('Error Triggering Alarm')
            await self.send_best_effort(self.settings.alarm_chat_ids(), 'Error Triggering Alarm')

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
            self.settings.arm_disarm_chat_ids(),
            f'Auto {state_str} on startup (scheduled {fire_time.strftime("%Y-%m-%d %H:%M")})'))

    async def auto_arm_disarm_timer(self):
        check_if_active_time = 1
        # Only the first pass, matching how this used to be called once before the loop:
        # enabling the timers later in the day should not snap the armed state around.
        startup_state_pending = True
        while True:
            # Re-read every pass rather than once: the schedule lives in the database, so
            # a timer added or disabled from the panel has to be picked up here. The store
            # hands back the same AutoArm objects while their configuration is unchanged,
            # which is what keeps check_action() able to fire at all.
            if self.settings.get('timers_enabled'):
                timerlist = self.settings.autoarm_timers()
                if startup_state_pending:
                    await self.apply_startup_arm_state(timerlist)
                for timer in timerlist:
                    action = timer.check_action()
                    if action is None:
                        continue
                    mainlogger.info(f'{timer} triggered')
                    self.set_armed(1 if action else 0)
                    await self.send_best_effort(
                        self.settings.arm_disarm_chat_ids(),
                        'Auto Armed' if action else 'Auto Disarmed')
            startup_state_pending = False
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
                    CallbackQueryHandler(self.admin_menu, pattern='^admin_menu_show$'),
                    CallbackQueryHandler(self.user_management_entry, pattern='^user_management_show$'),
                    CallbackQueryHandler(self.user_management_add,
                                         pattern=f'^user_management_add_({ROLE_PATTERN})$'),
                    CallbackQueryHandler(self.user_management_remove,
                                         pattern=f'^user_management_(remove|del)_({ROLE_PATTERN})(_-?\\d+)?$'),
                    CallbackQueryHandler(self.stream_management_entry, pattern='^stream_management_show$'),
                    CallbackQueryHandler(self.stream_add, pattern='^stream_add$'),
                    CallbackQueryHandler(self.stream_show, pattern=r'^stream_show_\d+$'),
                    CallbackQueryHandler(self.stream_edit, pattern=r'^stream_edit_\d+_\w+$'),
                    CallbackQueryHandler(self.stream_toggle, pattern=r'^stream_toggle_\d+_\w+$'),
                    CallbackQueryHandler(
                        self.stream_classes_entry,
                        pattern=rf'^stream_classes_\d+_({CLASS_GROUP_PATTERN})$'),
                    CallbackQueryHandler(
                        self.stream_class_toggle,
                        pattern=rf'^stream_class_\d+_({CLASS_GROUP_PATTERN})_\d+$'),
                    CallbackQueryHandler(self.stream_remove, pattern=r'^stream_remove_\d+$'),
                    CallbackQueryHandler(self.stream_delete, pattern=r'^stream_delete_\d+$'),
                    CallbackQueryHandler(self.system_management_entry, pattern='^system_management_show$'),
                    CallbackQueryHandler(self.restart_docker, pattern='^restart_docker$'),
                    CallbackQueryHandler(self.timer_entry, pattern='^timer_management_show$'),
                    CallbackQueryHandler(self.set_timer_enabled,
                                         pattern=r'^timer_management_(enable|disable)_\d+$'),
                    CallbackQueryHandler(self.timer_add, pattern='^timer_add_(arm|disarm)$'),
                    CallbackQueryHandler(self.timer_show, pattern=r'^timer_show_\d+$'),
                    CallbackQueryHandler(self.timer_edit, pattern=r'^timer_edit_\d+_(time|repeat)$'),
                    CallbackQueryHandler(self.timer_toggle, pattern=r'^timer_toggle_\d+$'),
                    CallbackQueryHandler(self.timer_action_toggle, pattern=r'^timer_action_\d+$'),
                    CallbackQueryHandler(self.timer_day_toggle, pattern=r'^timer_day_\d+_\d+$'),
                    CallbackQueryHandler(self.timer_remove, pattern=r'^timer_remove_\d+$'),
                    CallbackQueryHandler(self.timer_delete, pattern=r'^timer_delete_\d+$'),
                    CallbackQueryHandler(self.settings_entry, pattern='^settings_show$'),
                    CallbackQueryHandler(self.settings_category, pattern='^settings_category_.*$'),
                    CallbackQueryHandler(self.settings_toggle, pattern='^settings_toggle_.*$'),
                    CallbackQueryHandler(self.settings_edit, pattern='^settings_edit_.*$'),
                ],
                'add_member_text_input': [
                    MessageHandler(filters.TEXT & ~(filters.COMMAND | filters.Regex("^exit admin$")),
                                   self.user_management_add),
                ],
                'setting_text_input': [
                    MessageHandler(filters.TEXT & ~(filters.COMMAND | filters.Regex("^exit admin$")),
                                   self.settings_edit_input),
                ],
                # The stream and timer editors both start a text prompt from a button and
                # come back to an inline keyboard, so each needs a state of its own to
                # know which of the two typed answers it is reading.
                'stream_text_input': [
                    MessageHandler(filters.TEXT & ~(filters.COMMAND | filters.Regex("^exit admin$")),
                                   self.stream_edit_input),
                ],
                'timer_text_input': [
                    MessageHandler(filters.TEXT & ~(filters.COMMAND | filters.Regex("^exit admin$")),
                                   self.timer_edit_input),
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
    get_store(Settings.db_file).prepare()
    streaminfos = {
        0:{'armed':mp.Value('i', 1), 'alarm':mp.Value('i', 1),
           'timer_state_applied':mp.Value('i', 0)},
        1:{'armed':mp.Value('i', 1)}
    }
    bot = Telegrambot(streaminfos, mp.Queue())
    bot.run()

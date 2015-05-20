import json
import logging
import re
import time
import sys
from errbot import holder
from errbot import PY3
from errbot.backends.base import (
    Message, build_message, Identifier, Presence, ONLINE, OFFLINE,
    MUCRoom, MUCOccupant, RoomDoesNotExistError
)
from errbot.errBot import ErrBot
from errbot.utils import deprecated

try:
    from functools import lru_cache
except ImportError:
    from backports.functools_lru_cache import lru_cache
try:
    from slackclient import SlackClient
except ImportError:
    logging.exception("Could not start the Slack back-end")
    logging.fatal(
        "You need to install the slackclient package in order to use the Slack "
        "back-end. You should be able to install this package using: "
        "pip install slackclient"
    )
    sys.exit(1)
except SyntaxError:
    if not PY3:
        raise
    logging.exception("Could not start the Slack back-end")
    logging.fatal(
        "I cannot start the Slack back-end because I cannot import the SlackClient. "
        "Python 3 compatibility on SlackClient is still quite young, you may be "
        "running an old version or perhaps they released a version with a Python "
        "3 regression. As a last resort to fix this, you could try installing the "
        "latest master version from them using: "
        "pip install --upgrade https://github.com/slackhq/python-slackclient/archive/master.zip"
    )
    sys.exit(1)


# The Slack client automatically turns a channel name into a clickable
# link if you prefix it with a #. Other clients receive this link as a
# token matching this regex.
SLACK_CLIENT_CHANNEL_HYPERLINK = re.compile(r'^<#(?P<id>(C|G)[0-9A-Z]+)>$')


class SlackAPIResponseError(RuntimeError):
    """Slack API returned a non-OK response"""


class SlackBackend(ErrBot):

    def __init__(self, config):
        super().__init__(config)
        identity = config.BOT_IDENTITY
        self.token = identity.get('token', None)
        if not self.token:
            logging.fatal(
                'You need to set your token (found under "Bot Integration" on Slack) in '
                'the BOT_IDENTITY setting in your configuration. Without this token I '
                'cannot connect to Slack.'
            )
            sys.exit(1)
        self.sc = SlackClient(self.token)

        logging.debug("Verifying authentication token")
        self.auth = self.api_call("auth.test", raise_errors=False)
        if not self.auth['ok']:
            logging.fatal("Couldn't authenticate with Slack. Server said: %s" % self.auth['error'])
            sys.exit(1)
        logging.debug("Token accepted")
        self.jid = Identifier(node=self.auth["user_id"], resource=self.auth["user_id"])

    def api_call(self, method, data=None, raise_errors=True):
        """
        Make an API call to the Slack API and return response data.

        This is a thin wrapper around `SlackClient.server.api_call`.

        :param method:
            The API method to invoke (see https://api.slack.com/methods/).
        :param raise_errors:
            Whether to raise :class:`~SlackAPIResponseError` if the API
            returns an error
        :param data:
            A dictionary with data to pass along in the API request.
        :returns:
            The JSON-decoded API response
        :raises:
            :class:`~SlackAPIResponseError` if raise_errors is True and the
            API responds with `{"ok": false}`
        """
        if data is None:
            data = {}
        response = json.loads(self.sc.server.api_call(method, **data).decode('utf-8'))
        if raise_errors and not response['ok']:
            raise SlackAPIResponseError("Slack API call to %s failed: %s" % (method, response['error']))
        return response

    def serve_forever(self):
        logging.info("Connecting to Slack real-time-messaging API")
        if self.sc.rtm_connect():
            logging.info("Connected")
            try:
                while True:
                    events = self.sc.rtm_read()
                    for event in events:
                        try:
                            self._handle_slack_event(event)
                        except Exception:
                            logging.exception("An exception occurred while handling a Slack event")
                    time.sleep(1)
            except KeyboardInterrupt:
                logging.info("Caught KeyboardInterrupt, shutting down..")
            finally:
                logging.debug("Trigger disconnect callback")
                self.disconnect_callback()
                logging.debug("Trigger shutdown")
                self.shutdown()

        else:
            raise Exception('Connection failed, invalid token ?')

    def _handle_slack_event(self, event):
        """
        Act on a Slack event from the RTM stream
        """
        logging.debug("Slack event: %s" % event)
        t = event.get('type', None)
        if t == 'hello':
            self.connect_callback()
            self.callback_presence(Presence(identifier=self.jid, status=ONLINE))
        elif t == 'presence_change':
            idd = Identifier(node=event['user'])
            sstatus = event['presence']
            if sstatus == 'active':
                status = ONLINE
            else:
                status = OFFLINE  # TODO: all the cases

            self.callback_presence(Presence(identifier=idd, status=status))
        elif t == 'message':
            channel = event['channel']
            if channel.startswith('C'):
                logging.debug("Handling message from a public channel")
                message_type = 'groupchat'
            elif channel.startswith('G'):
                logging.debug("Handling message from a private group")
                message_type = 'groupchat'
            elif channel.startswith('D'):
                logging.debug("Handling message from a user")
                message_type = 'chat'
            else:
                logging.warning("Unknown message type! Unable to handle")
                return

            msg = Message(event['text'], type_=message_type)
            msg.frm = Identifier(
                node=self.userid_to_username(event['user']),
                domain=self.channelid_to_channelname(event['channel'])
            )
            msg.to = Identifier(
                node=self.sc.server.username,
                domain=self.channelid_to_channelname(event['channel'])
            )
            self.callback_message(msg)

    def userid_to_username(self, id):
        """Convert a Slack user ID to their user name"""
        return self.sc.server.users.find(id).name

    def username_to_userid(self, name):
        """Convert a Slack user name to their user ID"""
        return self.sc.server.users.find(name).id

    def channelid_to_channelname(self, id):
        """Convert a Slack channel ID to its channel name"""
        channel = self.sc.server.channels.find(id)
        if channel is None:
            raise RoomDoesNotExistError("No channel with ID %s exists" % id)
        return channel.name

    def channelname_to_channelid(self, name):
        """Convert a Slack channel name to its channel ID"""
        if name.startswith('#'):
            name = name[1:]
        channel = self.sc.server.channels.find(name)
        if channel is None:
            raise RoomDoesNotExistError("No channel named %s exists" % name)
        return channel.id

    def channels(self, exclude_archived=True, joined_only=False):
        """
        Get all channels and groups and return information about them.

        :param exclude_archived:
            Exclude archived channels/groups
        :param joined_only:
            Filter out channels the bot hasn't joined
        :returns:
            A list of channel (https://api.slack.com/types/channel)
            and group (https://api.slack.com/types/group) types.

        See also:
          * https://api.slack.com/methods/channels.list
          * https://api.slack.com/methods/groups.list
        """
        response = self.api_call('channels.list', data={'exclude_archived': exclude_archived})
        channels = [channel for channel in response['channels']
                    if channel['is_member'] or not joined_only]

        response = self.api_call('groups.list', data={'exclude_archived': exclude_archived})
        # No need to filter for 'is_member' in this next call (it doesn't
        # (even exist) because leaving a group means you have to get invited
        # back again by somebody else.
        groups = [group for group in response['groups']]

        return channels + groups

    @lru_cache(50)
    def get_im_channel(self, id):
        """Open a direct message channel to a user"""
        response = self.api_call('im.open', data={'user': id})
        return response['channel']['id']

    def send_message(self, mess):
        super().send_message(mess)
        to_humanreadable = "<unknown>"
        try:
            if mess.type == 'groupchat':
                to_humanreadable = mess.to.domain
                to_id = self.channelname_to_channelid(to_humanreadable)
            else:
                to_humanreadable = mess.to.node
                to_id = self.get_im_channel(self.username_to_userid(to_humanreadable))
            logging.debug('Sending %s message to %s (%s)' % (mess.type, to_humanreadable, to_id))
            self.sc.rtm_send_message(to_id, mess.body)
        except Exception:
            logging.exception(
                "An exception occurred while trying to send the following message "
                "to %s: %s" % (to_humanreadable, mess.body)
            )

    def build_message(self, text):
        return build_message(text, Message)

    def build_reply(self, mess, text=None, private=False):
        msg_type = mess.type
        response = self.build_message(text)

        response.frm = self.jid
        if msg_type == "groupchat" and private:
            response.to = mess.frm.node
        else:
            response.to = mess.frm
        response.type = 'chat' if private else msg_type

        return response

    def is_admin(self, usr):
        return usr.split('@')[0] in self.bot_config.BOT_ADMINS

    def shutdown(self):
        super().shutdown()

    @deprecated
    def join_room(self, room, username=None, password=None):
        return self.query_room(room)

    @property
    def mode(self):
        return 'slack'

    def query_room(self, room):
        if room.startswith('C') or room.startswith('G'):
            return SlackRoom(domain=room)

        m = SLACK_CLIENT_CHANNEL_HYPERLINK.match(room)
        if m is not None:
            return SlackRoom(domain=m.groupdict()['id'])

        return SlackRoom(name=room)

    def rooms(self):
        """
        Return a list of rooms the bot is currently in.

        :returns:
            A list of :class:`~SlackRoom` instances.
        """
        channels = self.channels(joined_only=True, exclude_archived=True)
        return [SlackRoom(domain=channel['id']) for channel in channels]

    def groupchat_reply_format(self):
        return '{0} {1}'


class SlackRoom(MUCRoom):
    def __init__(self, jid=None, node='', domain='', resource='', name=None):
        if jid is not None or node != '' or resource != '':
            raise ValueError("SlackRoom() only supports construction using domain or name")
        if domain != '' and name is not None:
            raise ValueError("domain and name are mutually exclusive")

        if name is not None:
            if name.startswith('#'):
                self._name = name[1:]
            else:
                self._name = name
        else:
            self._name = holder.bot.channelid_to_channelname(domain)

        self._id = None
        self.sc = holder.bot.sc

    def __str__(self):
        return "#%s" % self.name

    @property
    def _channel(self):
        """
        The channel object exposed by SlackClient
        """
        id = holder.bot.sc.server.channels.find(self.name)
        if id is None:
            raise RoomDoesNotExistError(
                "%s does not exist (or is a private group you don't have access to)" % str(self)
            )
        return id

    @property
    def _channel_info(self):
        """
        Channel info as returned by the Slack API.

        See also:
          * https://api.slack.com/methods/channels.list
          * https://api.slack.com/methods/groups.list
        """
        if self.private:
            return holder.bot.api_call('groups.info', data={'channel': self.id})["group"]
        else:
            return holder.bot.api_call('channels.info', data={'channel': self.id})["channel"]

    @property
    def private(self):
        """Return True if the room is a private group"""
        return self._channel.id.startswith('G')

    @property
    def id(self):
        """Return the ID of this room"""
        if self._id is None:
            self._id = self._channel.id
        return self._id

    @property
    def name(self):
        """Return the name of this room"""
        return self._name

    def join(self, username=None, password=None):
        logging.info("Joining channel %s" % str(self))
        holder.bot.api_call('channels.join', data={'name': self.name})

    def leave(self, reason=None):
        if self.id.startswith('C'):
            logging.info("Leaving channel %s (%s)" % (str(self), self.id))
            holder.bot.api_call('channels.leave', data={'channel': self.id})
        else:
            logging.info("Leaving group %s (%s)" % (str(self), self.id))
            holder.bot.api_call('groups.leave', data={'channel': self.id})
        self._id = None

    def create(self, private=False):
        if private:
            logging.info("Creating group %s" % str(self))
            holder.bot.api_call('groups.create', data={'name': self.name})
        else:
            logging.info("Creating channel %s" % str(self))
            holder.bot.api_call('channels.create', data={'name': self.name})

    def destroy(self):
        if self.id.startswith('C'):
            logging.info("Archiving channel %s (%s)" % (str(self), self.id))
            holder.bot.api_call('channels.archive', data={'channel': self.id})
        else:
            logging.info("Archiving group %s (%s)" % (str(self), self.id))
            holder.bot.api_call('groups.archive', data={'channel': self.id})
        self._id = None

    @property
    def exists(self):
        channels = holder.bot.channels(joined_only=False, exclude_archived=False)
        return len([c for c in channels if c['name'] == self.name]) > 0

    @property
    def joined(self):
        channels = holder.bot.channels(joined_only=True)
        return len([c for c in channels if c['name'] == self.name]) > 0

    @property
    def topic(self):
        if self._channel_info['topic']['value'] == '':
            return None
        else:
            return self._channel_info['topic']['value']

    @topic.setter
    def topic(self, topic):
        if self.private:
            logging.info("Setting topic of %s (%s) to '%s'" % (str(self), self.id, topic))
            holder.bot.api_call('groups.setTopic', data={'channel': self.id, 'topic': topic})
        else:
            logging.info("Setting topic of %s (%s) to '%s'" % (str(self), self.id, topic))
            holder.bot.api_call('channels.setTopic', data={'channel': self.id, 'topic': topic})

    @property
    def purpose(self):
        if self._channel_info['purpose']['value'] == '':
            return None
        else:
            return self._channel_info['purpose']['value']

    @purpose.setter
    def purpose(self, purpose):
        if self.private:
            logging.info("Setting purpose of %s (%s) to '%s'" % (str(self), self.id, purpose))
            holder.bot.api_call('groups.setPurpose', data={'channel': self.id, 'purpose': purpose})
        else:
            logging.info("Setting purpose of %s (%s) to '%s'" % (str(self), self.id, purpose))
            holder.bot.api_call('channels.setPurpose', data={'channel': self.id, 'purpose': purpose})

    @property
    def occupants(self):
        return [MUCOccupant("Somebody")]

    def invite(self, *args):
        pass
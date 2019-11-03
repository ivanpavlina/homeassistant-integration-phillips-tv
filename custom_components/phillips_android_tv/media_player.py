"""Phillips TV"""
import homeassistant.helpers.config_validation as cv
import json
import logging
import time
import voluptuous as vol
import wakeonlan

from datetime import timedelta
from homeassistant.components.media_player import (
    MediaPlayerDevice, PLATFORM_SCHEMA
)
from homeassistant.components.media_player.const import (
    SUPPORT_STOP, SUPPORT_PLAY, SUPPORT_NEXT_TRACK, SUPPORT_PAUSE,
    SUPPORT_PREVIOUS_TRACK, SUPPORT_VOLUME_SET, SUPPORT_TURN_OFF,
    SUPPORT_TURN_ON, SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_STEP,
    SUPPORT_SELECT_SOURCE, SUPPORT_PLAY_MEDIA
)
from homeassistant.const import (
    CONF_HOST, CONF_MAC, CONF_NAME, CONF_USERNAME, CONF_PASSWORD, STATE_OFF,
    STATE_ON, STATE_IDLE, STATE_UNKNOWN, STATE_PLAYING, STATE_PAUSED
)
from homeassistant.util import Throttle
from requests import Session
from requests.auth import HTTPDigestAuth
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException

# Workaround to suppress warnings about SSL certificates in Home Assistant log
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_LOGGER = logging.getLogger(__name__)

CONF_FAV_ONLY = 'favorite_channels_only'
CONF_TURN_ON_SWITCH = 'turn_on_switch'
CONF_TURN_OFF_SWITCH = 'turn_off_switch'
CONF_CAST_ENTITY = 'cast_entity'

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=5)

SUPPORT_PHILIPS_2016 = SUPPORT_TURN_OFF | SUPPORT_TURN_ON | \
                       SUPPORT_VOLUME_STEP | SUPPORT_VOLUME_MUTE | \
                       SUPPORT_VOLUME_SET | SUPPORT_NEXT_TRACK | \
                       SUPPORT_PREVIOUS_TRACK | SUPPORT_PAUSE | \
                       SUPPORT_PLAY | SUPPORT_STOP | SUPPORT_SELECT_SOURCE | \
                       SUPPORT_PLAY_MEDIA

DEFAULT_DEVICE = 'default'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_MAC = 'aa:aa:aa:aa:aa:aa'
DEFAULT_USER = 'user'
DEFAULT_PASS = 'pass'
DEFAULT_NAME = 'Phillips TV'
BASE_URL = 'https://{0}:1926/6/{1}'
TIMEOUT = 5.0
CONNFAILCOUNT = 5

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST, default=DEFAULT_HOST): cv.string,
    vol.Required(CONF_MAC, default=DEFAULT_MAC): cv.string,
    vol.Required(CONF_USERNAME, default=DEFAULT_USER): cv.string,
    vol.Required(CONF_PASSWORD, default=DEFAULT_PASS): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_FAV_ONLY, default=False): cv.boolean,
    vol.Optional(CONF_TURN_ON_SWITCH, default=''): cv.string,
    vol.Optional(CONF_TURN_OFF_SWITCH, default=''): cv.string,
    vol.Optional(CONF_CAST_ENTITY, default=''): cv.string
})

# pylint: disable=unused-argument
def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Phillips 2016+ TV platform."""
    name = config.get(CONF_NAME)
    host = config.get(CONF_HOST)
    mac = config.get(CONF_MAC)
    user = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    favorite_only = config.get(CONF_FAV_ONLY)
    custom_turn_on_switch = config.get(CONF_TURN_ON_SWITCH)
    custom_turn_off_switch = config.get(CONF_TURN_OFF_SWITCH)
    custom_cast_entity = config.get(CONF_CAST_ENTITY)
    tvapi = PhillipsTVBase(hass, host, user, password, favorite_only)
    add_devices([PhillipsTV(hass, tvapi, name, mac, custom_turn_on_switch, custom_turn_off_switch, custom_cast_entity)])


class PhillipsTV(MediaPlayerDevice):
    """Representation of a 2016+ Phillips TV exposing the JointSpace API."""

    def __init__(self, hass, tv, name, mac, custom_turn_on_switch, custom_turn_off_switch, custom_cast_entity):
        """Initialize the TV."""
        self._hass = hass
        self._tv = tv
        self._default_name = name
        self._name = name
        self._mac = mac
        self._wol = wakeonlan
        self._state = STATE_UNKNOWN
        self._on = False
        self._api_online = False
        self._min_volume = 0
        self._max_volume = 60
        self._volume = 0
        self._muted = False
        self._channel_id = ''
        self._channel_name = ''
        self._connfail = 0
        self._source = ''
        self._source_list = []
        self._media_cont_type = ''
        self._app_name = ''
        self._custom_turn_on_switch = custom_turn_on_switch
        self._custom_turn_off_switch = custom_turn_off_switch
        self._custom_cast_entity = custom_cast_entity


    @property
    def name(self):
        """Return the device name."""
        return self._name

    @property
    def should_poll(self):
        """Device should be polled."""
        return True

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_PHILIPS_2016

    @property
    def state(self):
        """Get the device state. An exception means OFF state."""
        return self._state

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return self._volume / self._max_volume

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        return self._muted

    def turn_off(self):
        """Turn off the device."""

        # Check if entity exists on event. Can't check on init because entity could not be set up yet
        if self._custom_turn_off_switch and not self._hass.states.get(self._custom_turn_off_switch):
            _LOGGER.warning("Switch entity %s not registered in homeassistant, defaulting to API turn_off call", self._custom_turn_off_switch)
            self._custom_turn_off_switch = None

        if self._custom_turn_off_switch:
            _LOGGER.info("Turning off custom switch [%s]", self._custom_turn_off_switch)
            self.call_hass_service('switch', 'turn_off', self._custom_turn_off_switch)
        else:
            _LOGGER.info("Setting powerstate off")
            self._tv.set_power_state('Standby')
        self.update()

    def turn_on(self):
        """Turn on the device."""

        # Check if entity exists on event. Can't check on init because entity could not be set up yet
        if self._custom_turn_on_switch and not self._hass.states.get(self._custom_turn_on_switch):
            _LOGGER.warning("Switch entity %s not registered in homeassistant, defaulting to API turn_on call", self._custom_turn_on_switch)
            self._custom_turn_on_switch = None

        if self._custom_turn_on_switch:
            _LOGGER.info("Turning on custom switch [%s]", self._custom_turn_on_switch)
            self.call_hass_service('switch', 'turn_on', self._custom_turn_on_switch)
        else:
            i = 0
            # TODO: This is blocking and self._tv.on will not change until 20 iterations are done
            while not self._tv.on and i < 20:
                if not self._api_online:
                    _LOGGER.info("Sending WOL: %s", i)
                    self.wol()
                _LOGGER.info("Setting powerstate: %s", i)
                self._tv.set_power_state('On')
                time.sleep(2)
                i += 1

    def play_media(self, media_type, media_id, **kwargs):
        if self._custom_cast_entity and not self._hass.states.get(self._custom_cast_entity):
            _LOGGER.warning("Cast entity %s not registered in homeassistant, turning off ability to play media", self._custom_cast_entity)
            self._custom_cast_entity = None

        if self._custom_cast_entity:
            self.call_hass_service('media_extractor', 'play_media', self._custom_cast_entity, {"media_content_id": media_id, "media_content_type": media_type})
            self._state = STATE_PLAYING
        else:
             _LOGGER.info("Cast entity not defined, cannot play media")


    def volume_up(self):
        """Send volume up command."""
        self._tv.send_key('VolumeUp')

    def volume_down(self):
        """Send volume down command."""
        self._tv.send_key('VolumeDown')

    def set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        tv_volume = volume * self._max_volume
        self._tv.set_volume(tv_volume)

    def mute_volume(self, mute):
        """Send mute command."""
        self._tv.send_key('Mute')

    def media_play(self):
        """Send media play command to media player."""
        self._tv.send_key('Play')
        self._state = STATE_PLAYING

    def media_play_pause(self):
        """Play or pause the media player."""
        if self._state in (STATE_PAUSED, STATE_IDLE):
            self.media_play()
        elif self._state == STATE_PLAYING:
            self.media_pause()

    def media_pause(self):
        """Send media pause command to media player."""
        self._tv.send_key('Pause')
        self._state = STATE_PAUSED

    def media_stop(self):
        """Send media stop command to media player."""
        self._tv.send_key('Stop')
        self._state = STATE_IDLE

    def media_next_track(self):
        """Send next track command."""
        if self.media_content_type == 'channel':
            self._tv.send_key('CursorUp')
        else:
            self._tv.send_key('FastForward')

    def media_previous_track(self):
        """Send the previous track command."""
        if self.media_content_type == 'channel':
            self._tv.send_key('CursorDown')
        else:
            self._tv.send_key('Rewind')

    @property
    def source(self):
        """Return the current input source."""
        return self._source

    def select_source(self, source):
        self._tv.change_source(source)
        self._source = source

    @property
    def media_title(self):
        """Title of current playing media."""
        if self.media_content_type == 'channel':
            return '{} - {}'.format(self._channel_id, self._channel_name)
        else:
            return self._channel_name

    @property
    def media_content_id(self):
        return self._channel_id

    @property
    def media_content_type(self):
        return self._media_cont_type

    @property
    def source_list(self):
        return self._source_list

    @property
    def app_name(self):
        return self._app_name

    def wol(self):
        if self._mac:
            self._wol.send_magic_packet(self._mac)

    def call_hass_service(self, domain, service, entity, additional_vars={}):
        vars = {"entity_id": entity}
        vars.update(additional_vars)
        self._hass.services.call(domain, service, vars)


    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Get the latest data and update device state."""
        self._tv.update()
        self._min_volume = self._tv.min_volume
        self._max_volume = self._tv.max_volume
        self._source_list = self._tv.app_source_list + self._tv.channel_source_list
        self._source = self._tv.app_name + ' ' + self._tv.channel_name
        self._channel_id = self._tv.channel_id
        self._channel_name = self._tv.channel_name
        self._media_cont_type = self._tv.media_content_type
        self._volume = self._tv.volume
        self._muted = self._tv.muted
        self._name = self._default_name
        self._app_name = self._tv.app_name
        self._on = self._tv.on
        self._api_online = self._tv.api_online
        if self._tv.on:
            if self._media_cont_type == 'cast':
                self._state = STATE_PLAYING
                try:
                    if self._custom_cast_entity and self._hass.states.get(self._custom_cast_entity):
                        self._channel_id = self._hass.states.get(self._custom_cast_entity).attributes['media_content_id']
                except Exception as e:
                    _LOGGER.warning(e)
            elif self._state in (STATE_OFF, STATE_UNKNOWN) or self._media_cont_type != 'app':
                self._state = STATE_ON
            elif self._media_cont_type == 'app':
                self._state = STATE_IDLE
        else:
            self._state = STATE_OFF


class PhillipsTVBase(object):
    def __init__(self, hass, host, user, password, favorite_only):
        self._hass = hass
        self._host = host
        self._user = user
        self._password = password
        self._connfail = 0
        self.on = False
        self.api_online = False
        self.min_volume = 0
        self.max_volume = 60
        self.volume = 0
        self.muted = False
        self.favorite_only = favorite_only
        self.applications = {}
        self.app_source_list = []
        self.class_name_to_app = {}
        self.channels = {}
        self.channel_source_list = []
        self.channel_id = ''
        self.media_content_type = ''
        self.channel_name = ''
        self.app_name = ''
        # The XTV app appears to have a bug that limits the nummber of SSL session to 100
        # The code below forces the control to keep re-using a single connection
        self._session = Session()
        self._session.verify = False
        self._session.mount('https://', HTTPAdapter(pool_connections=1))

    def _get_req(self, path):
        try:
            if self._connfail:
                self._connfail -= 1
                return None
            resp = self._session.get(
                BASE_URL.format(self._host, path),
                verify=False,
                auth=HTTPDigestAuth(self._user, self._password),
                timeout=TIMEOUT)
            self.api_online = True
            return json.loads(resp.text)
        except json.JSONDecodeError:
            _LOGGER.warn(
                "TV is not returning JSON. Either the authentification failed or your TV does not support calling %s.",
                path)
            _LOGGER.info("Response of TV: %s", resp.text)
            return None
        except RequestException:
            self._connfail = CONNFAILCOUNT
            self.api_online = False
            return None

    def _post_req(self, path, data):
        try:
            if self._connfail:
                self._connfail -= 1
                return False
            resp = self._session.post(
                BASE_URL.format(self._host, path),
                data=json.dumps(data),
                verify=False,
                auth=HTTPDigestAuth(self._user, self._password),
                timeout=TIMEOUT)
            self.api_online = True
            if resp.status_code == 200:
                return True
            else:
                return False
        except RequestException:
            self._connfail = CONNFAILCOUNT
            self.api_online = False
            return False

    def update(self):
        self.get_state()
        self.get_applications()
        if self.favorite_only:
            self.get_favorite_channels()
        else:
            self.get_channels()
        self.get_audiodata()
        self.get_channel()

    def get_channel(self):
        if self.on:
            rr = self._get_req('activities/current')
            if rr:
                pkg_name = rr.get('component', {}).get('packageName', '')
                class_name = rr.get('component', {}).get('className', '')
                _LOGGER.info(f"{pkg_name} - {class_name}")
                if pkg_name in ('org.droidtv.zapster', 'org.droidtv.playtv','NA'):
                    if class_name in ('org.droidtv.playtv.PlayTvActivity', 'NA'):
                        self.media_content_type = 'app'
                        self.channel_name = 'PlayTV'
                        self.app_name = '📱'
                    else:
                        self.media_content_type = 'channel'
                        r = self._get_req('activities/tv')
                        if r:
                            self.channel_id = r.get('channel', {}).get('preset', 'N/A')
                            self.channel_name = r.get('channel', {}).get('name', 'N/A')
                            self.app_name = '📺'
                        else:
                            self.channel_name = 'N/A'
                            self.channel_id = 'N/A'
                            self.app_name = '📺'
                else:
                    self.media_content_type = 'app'
                    if pkg_name in ['com.google.android.leanbacklauncher', 'com.google.android.tvlauncher']:
                        self.app_name = '📱'
                        self.channel_name = 'Home'
                        #self.media_content_type = ''
                    elif pkg_name == 'com.google.android.apps.mediashell':
                        self.app_name = '📺'
                        self.channel_name = 'Casting'
                        self.media_content_type = 'cast'
                    elif pkg_name == 'org.droidtv.nettvbrowser':
                        self.app_name = '📱'
                        self.channel_name = 'Net TV Browser'
                    elif pkg_name == 'org.droidtv.settings':
                        self.app_name = self.class_name_to_app.get(class_name, {}).get('label', class_name) if class_name != 'NA' else ''
                        self.channel_name = 'Settings'
                    else:
                        app = self.class_name_to_app.get(class_name, {})
                        if 'label' in app:
                            self.app_name = '📱'
                            self.channel_name = app['label']
                        else:
                            self.app_name = class_name
                            self.channel_name = pkg_name

    def get_channels(self):
        r = self._get_req('channeldb/tv/channelLists/all')
        if r:
            self.channels = dict(sorted({chn['name']: chn
                                         for chn in r['Channel']}.items(),
                                         key=lambda a: a[0].upper()))
            self.channel_source_list = ['📺 ' + channelName
                                        for channelName in self.channels.keys()]

    # Filtering out favorite channels here
    def get_favorite_channels(self):
        r = self._get_req('channeldb/tv/channelLists/all')
        favorite_res = self._get_req('channeldb/tv/favoriteLists/1')
        if r and favorite_res:
            self.channels = dict(
                sorted(
                    {chn['name']: chn
                     for chn in r['Channel']}.items(),
                    key=lambda a: a[0].upper()))
            all_channels = dict({chn['ccid']: chn
                                 for chn in r['Channel']}.items())
            favorite_channels = favorite_res.pop('channels')
            ccids = ([Channel['ccid'] for Channel in favorite_channels])
            fav_channel = {key: all_channels[key] for key in ccids}
            self.channel_source_list = []
            for fav_channel_ccid, fav_channel_ccinfo in fav_channel.items():
                self.channel_source_list.append('📺 ' +
                                                fav_channel_ccinfo['name'])
            self.channel_source_list.sort()
        else:
            _LOGGER.debug("Favorites not supported for this TV")
            return self.get_channels()

    def get_applications(self):
        r = self._get_req('applications')
        if r:
            home_app = {'label': 'Home',
                        'intent': {'component':
                                    {'packageName': 'com.google.android',
                                    'className': 'com.google.android.tvlauncher'},
                                    'action': 'android.intent.action.MAIN'},
                        'order': 0,
                        'id': 'com.google.android.tvlauncher-com.google.android',
                        'type': 'app'}

            self.class_name_to_app = {app['intent']['component']['className']: app
                                      for app in r['applications']}

            self.class_name_to_app['com.google.android.tvlauncher'] = home_app

            self.applications = dict(sorted({app['label']: app
                                             for app in r['applications']}.items(),
                                             key=lambda a: a[0].upper()))
            self.applications['Home'] = home_app

            self.app_source_list = ['📱 ' + appLabel
                                    for appLabel in self.applications.keys()]

    def change_source(self, source_label):
        if source_label:
            if source_label.startswith('📱'):
                app = self.applications[source_label[2:]]

                if app['label'] == 'Home':
                    self.send_key('Home')
                else:
                    self._post_req('activities/launch', app)
            elif source_label.startswith('📺'):
                chn = self.channels[source_label[2:]]
                data = {
                    'channel': {
                        'ccid': chn['ccid'],
                        'preset': chn['preset'],
                        'name': chn['name']
                    },
                    'channelList': {
                        'id': 'allter',
                        'version': '30'
                    }
                }
                self._post_req('activities/tv', data)

    def get_state(self):
        r = self._get_req('powerstate')
        if r:
            self.on = r['powerstate'] == 'On'
        else:
            self.on = False

    def get_audiodata(self):
        audiodata = self._get_req('audio/volume')
        if audiodata:
            self.min_volume = int(audiodata['min'])
            self.max_volume = int(audiodata['max'])
            self.volume = audiodata['current']
            self.muted = audiodata['muted']
        else:
            self.min_volume = None
            self.max_volume = None
            self.volume = None
            self.muted = None

    def set_volume(self, level):
        if level:
            if self.min_volume != 0 or not self.max_volume:
                self.get_audiodata()
            if not self.on:
                return
            try:
                targetlevel = int(level)
            except ValueError:
                return
            if (self.min_volume + 1) < targetlevel > self.max_volume:
                return
            self._post_req('audio/volume', {'current': targetlevel, 'muted': False})
            self.volume = targetlevel

    def set_power_state(self, state):
        self._post_req('powerstate', {'powerstate': state})

    def send_key(self, key):
        self._post_req('input/key', {'key': key})


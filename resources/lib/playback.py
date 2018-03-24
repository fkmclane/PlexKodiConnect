"""
Used to kick off Kodi playback
"""
from logging import getLogger
from threading import Thread

from xbmc import Player, sleep

from PlexAPI import API
from PlexFunctions import GetPlexMetadata, init_plex_playqueue
from downloadutils import DownloadUtils as DU
import plexdb_functions as plexdb
import kodidb_functions as kodidb
import playlist_func as PL
import playqueue as PQ
from playutils import PlayUtils
from PKC_listitem import PKC_ListItem
from pickler import pickle_me, Playback_Successful
import json_rpc as js
from utils import settings, dialog, language as lang, try_encode
from plexbmchelper.subscribers import LOCKER
import variables as v
import state

###############################################################################
LOG = getLogger("PLEX." + __name__)
# Do we need to return ultimately with a setResolvedUrl?
RESOLVE = True
###############################################################################


@LOCKER.lockthis
def playback_triage(plex_id=None, plex_type=None, path=None, resolve=True):
    """
    Hit this function for addon path playback, Plex trailers, etc.
    Will setup playback first, then on second call complete playback.

    Will set Playback_Successful() with potentially a PKC_ListItem() attached
    (to be consumed by setResolvedURL in default.py)

    If trailers or additional (movie-)parts are added, default.py is released
    and a completely new player instance is called with a new playlist. This
    circumvents most issues with Kodi & playqueues

    Set resolve to False if you do not want setResolvedUrl to be called on
    the first pass - e.g. if you're calling this function from the original
    service.py Python instance
    """
    LOG.info('playback_triage called with plex_id %s, plex_type %s, path %s',
             plex_id, plex_type, path)
    global RESOLVE
    RESOLVE = resolve
    if not state.AUTHENTICATED:
        LOG.error('Not yet authenticated for PMS, abort starting playback')
        # "Unauthorized for PMS"
        dialog('notification', lang(29999), lang(30017))
        _ensure_resolve(abort=True)
        return
    playqueue = PQ.get_playqueue_from_type(
        v.KODI_PLAYLIST_TYPE_FROM_PLEX_TYPE[plex_type])
    pos = js.get_position(playqueue.playlistid)
    # Can return -1 (as in "no playlist")
    pos = pos if pos != -1 else 0
    LOG.debug('playQueue position: %s for %s', pos, playqueue)
    # Have we already initiated playback?
    try:
        item = playqueue.items[pos]
    except IndexError:
        initiate = True
    else:
        initiate = True if item.plex_id != plex_id else False
    if initiate:
        _playback_init(plex_id, plex_type, playqueue, pos)
    else:
        # kick off playback on second pass
        _conclude_playback(playqueue, pos)


def _playback_init(plex_id, plex_type, playqueue, pos):
    """
    Playback setup if Kodi starts playing an item for the first time.
    """
    LOG.info('Initializing PKC playback')
    xml = GetPlexMetadata(plex_id)
    try:
        xml[0].attrib
    except (IndexError, TypeError, AttributeError):
        LOG.error('Could not get a PMS xml for plex id %s', plex_id)
        # "Play error"
        dialog('notification', lang(29999), lang(30128), icon='{error}')
        _ensure_resolve(abort=True)
        return
    if playqueue.kodi_pl.size() > 1:
        # Special case - we already got a filled Kodi playqueue
        try:
            _init_existing_kodi_playlist(playqueue)
        except PL.PlaylistError:
            LOG.error('Aborting playback_init for longer Kodi playlist')
            _ensure_resolve(abort=True)
            return
        # Now we need to use setResolvedUrl for the item at position pos
        _conclude_playback(playqueue, pos)
        return
    # "Usual" case - consider trailers and parts and build both Kodi and Plex
    # playqueues
    # Fail the item we're trying to play now so we can restart the player
    _ensure_resolve()
    api = API(xml[0])
    trailers = False
    if (plex_type == v.PLEX_TYPE_MOVIE and not api.resume_point() and
            settings('enableCinema') == "true"):
        if settings('askCinema') == "true":
            # "Play trailers?"
            trailers = dialog('yesno', lang(29999), lang(33016))
            trailers = True if trailers else False
        else:
            trailers = True
    LOG.debug('Playing trailers: %s', trailers)
    playqueue.clear()
    if plex_type != v.PLEX_TYPE_CLIP:
        # Post to the PMS to create a playqueue - in any case due to Companion
        xml = init_plex_playqueue(plex_id,
                                  xml.attrib.get('librarySectionUUID'),
                                  mediatype=plex_type,
                                  trailers=trailers)
        if xml is None:
            LOG.error('Could not get a playqueue xml for plex id %s, UUID %s',
                      plex_id, xml.attrib.get('librarySectionUUID'))
            # "Play error"
            dialog('notification', lang(29999), lang(30128), icon='{error}')
            _ensure_resolve(abort=True)
            return
        # Should already be empty, but just in case
        PL.get_playlist_details_from_xml(playqueue, xml)
    stack = _prep_playlist_stack(xml)
    # Sleep a bit to let setResolvedUrl do its thing - bit ugly
    sleep(200)
    _process_stack(playqueue, stack)
    # Always resume if playback initiated via PMS and there IS a resume
    # point
    offset = api.resume_point() * 1000 if state.CONTEXT_MENU_PLAY else None
    # Reset some playback variables
    state.CONTEXT_MENU_PLAY = False
    state.FORCE_TRANSCODE = False
    # Do NOT set offset, because Kodi player will return here to resolveURL
    # New thread to release this one sooner (e.g. harddisk spinning up)
    thread = Thread(target=threaded_playback,
                    args=(playqueue.kodi_pl, pos, offset))
    thread.setDaemon(True)
    LOG.info('Done initializing playback, starting Kodi player at pos %s and '
             'resume point %s', pos, offset)
    # By design, PKC will start Kodi playback using Player().play(). Kodi
    # caches paths like our plugin://pkc. If we use Player().play() between
    # 2 consecutive startups of exactly the same Kodi library item, Kodi's
    # cache will have been flushed for some reason. Hence the 2nd call for
    # plugin://pkc will be lost; Kodi will try to startup playback for an empty
    # path: log entry is "CGUIWindowVideoBase::OnPlayMedia <missing path>"
    thread.start()


def _ensure_resolve(abort=False):
    """
    Will check whether RESOLVE=True and if so, fail Kodi playback startup
    with the path 'PKC_Dummy_Path_Which_Fails' using setResolvedUrl (and some
    pickling)

    This way we're making sure that other Python instances (calling default.py)
    will be destroyed.
    """
    if RESOLVE:
        LOG.debug('Passing dummy path to Kodi')
        if not state.CONTEXT_MENU_PLAY:
            # Because playback won't start with context menu play
            state.PKC_CAUSED_STOP = True
        result = Playback_Successful()
        result.listitem = PKC_ListItem(path='PKC_Dummy_Path_Which_Fails')
        pickle_me(result)
    if abort:
        # Reset some playback variables
        state.CONTEXT_MENU_PLAY = False
        state.FORCE_TRANSCODE = False
        state.RESUME_PLAYBACK = False


def _init_existing_kodi_playlist(playqueue):
    """
    Will take the playqueue's kodi_pl with MORE than 1 element and initiate
    playback (without adding trailers)
    """
    LOG.debug('Kodi playlist size: %s', playqueue.kodi_pl.size())
    for i, kodi_item in enumerate(js.playlist_get_items(playqueue.playlistid)):
        if i == 0:
            item = PL.init_Plex_playlist(playqueue, kodi_item=kodi_item)
        else:
            item = PL.add_item_to_PMS_playlist(playqueue,
                                               i,
                                               kodi_item=kodi_item)
        item.force_transcode = state.FORCE_TRANSCODE
    LOG.debug('Done building Plex playlist from Kodi playlist')


def _prep_playlist_stack(xml):
    stack = []
    for item in xml:
        api = API(item)
        if (state.CONTEXT_MENU_PLAY is False and
                api.plex_type() not in (v.PLEX_TYPE_CLIP, v.PLEX_TYPE_EPISODE)):
            # If user chose to play via PMS or force transcode, do not
            # use the item path stored in the Kodi DB
            with plexdb.Get_Plex_DB() as plex_db:
                plex_dbitem = plex_db.getItem_byId(api.plex_id())
            kodi_id = plex_dbitem[0] if plex_dbitem else None
            kodi_type = plex_dbitem[4] if plex_dbitem else None
        else:
            # We will never store clips (trailers) in the Kodi DB.
            # Also set kodi_id to None for playback via PMS, so that we're
            # using add-on paths.
            # Also do NOT associate episodes with library items for addon paths
            # as artwork lookup is broken (episode path does not link back to
            # season and show)
            kodi_id = None
            kodi_type = None
        for part, _ in enumerate(item[0]):
            api.set_part_number(part)
            if kodi_id is None:
                # Need to redirect again to PKC to conclude playback
                path = ('plugin://%s/?plex_id=%s&plex_type=%s&mode=play'
                        % (v.ADDON_TYPE[api.plex_type()],
                           api.plex_id(),
                           api.plex_type()))
                listitem = api.create_listitem()
                listitem.setPath(try_encode(path))
            else:
                # Will add directly via the Kodi DB
                path = None
                listitem = None
            stack.append({
                'kodi_id': kodi_id,
                'kodi_type': kodi_type,
                'file': path,
                'xml_video_element': item,
                'listitem': listitem,
                'part': part,
                'playcount': api.viewcount(),
                'offset': api.resume_point(),
                'id': api.item_id()
            })
    return stack


def _process_stack(playqueue, stack):
    """
    Takes our stack and adds the items to the PKC and Kodi playqueues.
    """
    # getposition() can return -1
    pos = max(playqueue.kodi_pl.getposition(), 0) + 1
    for item in stack:
        if item['kodi_id'] is None:
            playlist_item = PL.add_listitem_to_Kodi_playlist(
                playqueue,
                pos,
                item['listitem'],
                file=item['file'],
                xml_video_element=item['xml_video_element'])
        else:
            # Directly add element so we have full metadata
            playlist_item = PL.add_item_to_kodi_playlist(
                playqueue,
                pos,
                kodi_id=item['kodi_id'],
                kodi_type=item['kodi_type'],
                xml_video_element=item['xml_video_element'])
        playlist_item.playcount = item['playcount']
        playlist_item.offset = item['offset']
        playlist_item.part = item['part']
        playlist_item.id = item['id']
        playlist_item.force_transcode = state.FORCE_TRANSCODE
        pos += 1


def _conclude_playback(playqueue, pos):
    """
    ONLY if actually being played (e.g. at 5th position of a playqueue).

        Decide on direct play, direct stream, transcoding
        path to
            direct paths: file itself
            PMS URL
            Web URL
        audiostream (e.g. let user choose)
        subtitle stream (e.g. let user choose)
        Init Kodi Playback (depending on situation):
            start playback
            return PKC listitem attached to result
    """
    LOG.info('Concluding playback for playqueue position %s', pos)
    result = Playback_Successful()
    listitem = PKC_ListItem()
    item = playqueue.items[pos]
    if item.xml is not None:
        # Got a Plex element
        api = API(item.xml)
        api.set_part_number(item.part)
        api.create_listitem(listitem)
        playutils = PlayUtils(api, item)
        playurl = playutils.getPlayUrl()
    else:
        playurl = item.file
    listitem.setPath(try_encode(playurl))
    if item.playmethod == 'DirectStream':
        listitem.setSubtitles(api.cache_external_subs())
    elif item.playmethod == 'Transcode':
        playutils.audio_subtitle_prefs(listitem)

    if state.RESUME_PLAYBACK is True:
        state.RESUME_PLAYBACK = False
        if (item.offset is None and
                item.plex_type not in (v.PLEX_TYPE_SONG, v.PLEX_TYPE_CLIP)):
            with plexdb.Get_Plex_DB() as plex_db:
                plex_dbitem = plex_db.getItem_byId(item.plex_id)
                file_id = plex_dbitem[1] if plex_dbitem else None
            with kodidb.GetKodiDB('video') as kodi_db:
                item.offset = kodi_db.get_resume(file_id)
        LOG.info('Resuming playback at %s', item.offset)
        listitem.setProperty('StartOffset', str(item.offset))
        listitem.setProperty('resumetime', str(item.offset))
    # Reset the resumable flag
    result.listitem = listitem
    pickle_me(result)
    LOG.info('Done concluding playback')


def process_indirect(key, offset, resolve=True):
    """
    Called e.g. for Plex "Play later" - Plex items where we need to fetch an
    additional xml for the actual playurl. In the PMS metadata, indirect="1" is
    set.

    Will release default.py with setResolvedUrl

    Set resolve to False if playback should be kicked off directly, not via
    setResolvedUrl
    """
    LOG.info('process_indirect called with key: %s, offset: %s', key, offset)
    global RESOLVE
    RESOLVE = resolve
    result = Playback_Successful()
    if key.startswith('http') or key.startswith('{server}'):
        xml = DU().downloadUrl(key)
    elif key.startswith('/system/services'):
        xml = DU().downloadUrl('http://node.plexapp.com:32400%s' % key)
    else:
        xml = DU().downloadUrl('{server}%s' % key)
    try:
        xml[0].attrib
    except (TypeError, IndexError, AttributeError):
        LOG.error('Could not download PMS metadata')
        _ensure_resolve(abort=True)
        return
    if offset != '0':
        offset = int(v.PLEX_TO_KODI_TIMEFACTOR * float(offset))
        # Todo: implement offset
    api = API(xml[0])
    listitem = PKC_ListItem()
    api.create_listitem(listitem)
    playqueue = PQ.get_playqueue_from_type(
        v.KODI_PLAYLIST_TYPE_FROM_PLEX_TYPE[api.plex_type()])
    playqueue.clear()
    item = PL.Playlist_Item()
    item.xml = xml[0]
    item.offset = int(offset)
    item.plex_type = v.PLEX_TYPE_CLIP
    item.playmethod = 'DirectStream'
    # Need to get yet another xml to get the final playback url
    xml = DU().downloadUrl('http://node.plexapp.com:32400%s'
                           % xml[0][0][0].attrib['key'])
    try:
        xml[0].attrib
    except (TypeError, IndexError, AttributeError):
        LOG.error('Could not download last xml for playurl')
        _ensure_resolve(abort=True)
        return
    playurl = xml[0].attrib['key']
    item.file = playurl
    listitem.setPath(try_encode(playurl))
    playqueue.items.append(item)
    if resolve is True:
        result.listitem = listitem
        pickle_me(result)
    else:
        thread = Thread(target=Player().play,
                        args={'item': try_encode(playurl),
                              'listitem': listitem})
        thread.setDaemon(True)
        LOG.info('Done initializing PKC playback, starting Kodi player')
        thread.start()


def play_xml(playqueue, xml, offset=None, start_plex_id=None):
    """
    Play all items contained in the xml passed in. Called by Plex Companion.

    Either supply the ratingKey of the starting Plex element. Or set
    playqueue.selectedItemID
    """
    LOG.info("play_xml called with offset %s, start_plex_id %s",
             offset, start_plex_id)
    stack = _prep_playlist_stack(xml)
    _process_stack(playqueue, stack)
    LOG.debug('Playqueue after play_xml update: %s', playqueue)
    if start_plex_id is not None:
        for startpos, item in enumerate(playqueue.items):
            if item.plex_id == start_plex_id:
                break
        else:
            startpos = 0
    else:
        for startpos, item in enumerate(playqueue.items):
            if item.id == playqueue.selectedItemID:
                break
        else:
            startpos = 0
    thread = Thread(target=threaded_playback,
                    args=(playqueue.kodi_pl, startpos, offset))
    LOG.info('Done play_xml, starting Kodi player at position %s', startpos)
    thread.start()


def threaded_playback(kodi_playlist, startpos, offset):
    """
    Seek immediately after kicking off playback is not reliable.
    """
    player = Player()
    player.play(kodi_playlist, None, False, startpos)
    if offset and offset != '0':
        i = 0
        while not player.isPlaying():
            sleep(100)
            i += 1
            if i > 100:
                LOG.error('Could not seek to %s', offset)
                return
        js.seek_to(int(offset))

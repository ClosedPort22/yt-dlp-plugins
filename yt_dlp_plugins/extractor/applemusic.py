import functools
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    NUMBER_RE,
    ExtractorError,
    dfxp2srt,
    int_or_none,
    parse_codecs,
    parse_qs,
    traverse_obj,
    unified_strdate,
    url_or_none,
    variadic,
)

# Apple uses non-standard time expressions in TTML lyrics such as '1:20.908',
# which yt-dlp cannot handle
# Ref:
# https://www.w3.org/TR/2005/WD-ttaf1-dfxp-20050321/#timing-value-timeExpression
# https://www.w3.org/TR/2018/REC-ttml1-20181108/#timing-value-timeExpression


def _parse_dfxp_time_expr_fix(time_expr):
    if not time_expr:
        return None

    if mobj := re.match(rf'^(?P<time_offset>{NUMBER_RE})s?$', time_expr):
        return float(mobj.group('time_offset'))

    if mobj := re.match(r'^(?:(\d+):)?(\d{1,2}):(\d\d(?:(?:\.|:)\d+)?)$', time_expr):
        return 3600 * int(mobj.group(1) or 0) + 60 * int(mobj.group(2)) + float(mobj.group(3).replace(':', '.'))

    return None


assert _parse_dfxp_time_expr_fix('00:01') == 1.0
assert _parse_dfxp_time_expr_fix('00:01:100') == 1.1
assert _parse_dfxp_time_expr_fix('00:01.100') == 1.1
assert _parse_dfxp_time_expr_fix('1:01:100') == 61.1
assert _parse_dfxp_time_expr_fix('1:01.100') == 61.1


dfxp2srt.__globals__['parse_dfxp_time_expr'] = _parse_dfxp_time_expr_fix


class AppleMusicBaseIE(InfoExtractor):
    _VALID_URL = False
    _VALID_URL_BASE = r'^https?://(?:(?:geo|beta)\.)?music\.apple\.com/'

    _SUPPRESS_AUTH = {'Authorization': '', 'Media-User-Token': ''}
    _SUPPRESS_USER_AUTH = {'Media-User-Token': ''}
    _api_headers = {'Origin': 'https://music.apple.com'}

    def __init__(self, downloader=None):
        super().__init__(downloader)

        def parse_codecs_patched(codecs_str):
            if codecs_str == 'alac':
                return {
                    'vcodec': 'none',
                    'acodec': codecs_str,
                    'dynamic_range': None,
                }
            return parse_codecs(codecs_str)

        # workaround for yt-dlp's format parser not recognizing 'alac'
        # (the selector does recognize it)
        self._parse_m3u8_formats_and_subtitles.__globals__['parse_codecs'] = parse_codecs_patched

    @functools.cached_property
    def _MAX_THUMBNAIL_WIDTH(self):
        return self._configuration_arg('max_thumbnail_width', ['12000'])[0]

    @functools.cached_property
    def _MAX_THUMBNAIL_HEIGHT(self):
        return self._configuration_arg('max_thumbnail_height', ['12000'])[0]

    @staticmethod
    def _get_lang_query(url):
        if lang := parse_qs(url).get('l'):
            return {'l': lang[0]}
        return None

    def _get_anonymous_token(self, video_id):
        webpage = self._download_webpage('https://beta.music.apple.com/', video_id, 'Retrieving anonymous token')
        path = self._search_regex(r'/(assets/index-legacy-[^/]+\.js)', webpage, name='path to JavaScript file')
        js = self._download_webpage(f'https://beta.music.apple.com/{path}', video_id, 'Downloading JavaScript file')
        return self._search_regex(r'"(eyJh[^"]+)', js, 'anonymous token')

    def _download_api_json(self, *args, expected_status=None, headers={}, **kwargs):
        kwargs.setdefault('transform_source', lambda x: x or '{}')
        # merge expected response codes
        codes = [401, *variadic(expected_status)] if expected_status else 401

        def request():
            res = self._download_json_handle(
                *args, expected_status=codes, headers={**self._api_headers, **headers}, **kwargs)
            if res is False:
                return res  # used to signal non-fatal errors
            result, urlh = res
            if urlh.status == 401:
                return None  # don't return yet
            return result  # normal result

        if self._api_headers.get('Authorization'):
            if (res := request()) is not None:
                return res
            # token expired
        elif cached_token := self.cache.load('applemusic', 'token'):
            self._api_headers['Authorization'] = f'Bearer {cached_token}'
            if (res := request()) is not None:
                return res
            # token expired

        # anonymous token has expired or is not cached
        token = self._get_anonymous_token(video_id=kwargs.get('video_id'))
        self.cache.store('applemusic', 'token', token)
        self._api_headers['Authorization'] = f'Bearer {token}'

        # should not return 401
        return self._download_json(*args, expected_status=expected_status,
                                   headers={**self._api_headers, **headers}, **kwargs)

    def _extract_thumbnail(self, obj):
        return traverse_obj(obj, {
            'url': ('url', {url_or_none}, {lambda x: x.replace(
                    '{w}x{h}', f'{self._MAX_THUMBNAIL_WIDTH}x{self._MAX_THUMBNAIL_HEIGHT}')}),
            # XXX: remove these if max width/height is specified?
            'width': ('width', {int_or_none}),
            'height': ('height', {int_or_none}),
        })

    @staticmethod
    def _extract_common_metadata(obj):
        return traverse_obj(obj, ('attributes', {
            'title': ('name', {str}),
            'artist': ('artistName', {str}),  # XXX: deprecated by yt-dlp
            'release_date': ('releaseDate', {unified_strdate}),
            'age_limit': ('contentRating', {str}, {{'explicit': 18, 'clean': 0}.get}),
            'genres': ('genreNames', ..., {str}),
            # not part of yt-dlp
            'is_apple_digital_master': (('isAppleDigitalMaster', 'isMasteredForItunes'), {bool}, any),
        }))

    @staticmethod
    def _extract_album_metadata(album):
        data = traverse_obj(album, ('attributes', {
            'album_artist': ('artistName', {str}),
            'description': ('editorialNotes', ('standard', 'short'), {str}, any),
            # not part of yt-dlp
            'album_id': ('playParams', 'id', {str}),
            'upc': ('upc', {str}),
            'record_label': ('recordLabel', {str}),
            'copyright': ('copyright', {str}),
            # this doesn't necessarily equal to the number of tracks returned, since
            # some tracks may be unavailable
            'track_count': ('trackCount', {int_or_none}),
        }))
        for key, album_type in {
            'isCompilation': 'Compilation',
            'isSingle': 'Single',
        }.items():
            if traverse_obj(album, ('attributes', key, {bool})):
                data['album_type'] = album_type
                break
        return data


class AppleMusicIE(AppleMusicBaseIE):
    _VALID_URL = AppleMusicBaseIE._VALID_URL_BASE + (
        r'(?P<region>[a-z]{2})/'
        r'(?:song/.+/(?P<song_id>[0-9]+)|album/.+/(?P<album_id>[0-9]+).*'
        r'(?:\?|&)i=(?P<song_id_2>[0-9]+))')
    _TESTS = [{
        'url': 'https://music.apple.com/us/album/joyride/1754468855?i=1754468856',
        'info_dict': {
            'id': '1754468856',
            'ext': 'm4a',
        },
        'params': {'skip_download': True},
        'expected_exception': 'ExtractorError',
    }, {
        'note': 'unplayable song',
        'url': 'https://music.apple.com/us/album/numb/1440843089?i=1440843092',
        'info_dict': {
            'id': '1440843092',
            'release_date': '20160210',
            'thumbnail': r're:^https://.+\.mzstatic\.com/image/thumb/.+1234x4321(?:bb)?\.jpg',
            'composers': ['Max Jury & Dean Josiah Cover'],
            'artists': ['Max Jury'],
            'upc': '00602547938022',
            'track_count': 9,
            'is_apple_digital_master': True,
            'track_number': 1,
            'disc_number': 1,
            'title': 'Numb',
            'genres': ['Alternative', 'Music'],
            'storefront_id': 143441,
            'album_type': 'Compilation',
            'isrc': 'GBX721500409',
            'album': 'Me Before You (Original Motion Picture Soundtrack)',
            'album_id': '1440843089',
            'copyright': 'This Compilation ℗ 2016 Interscope Records',
            'track': 'Numb',
            'album_artists': ['Various Artists'],
            'artist_ids': ['1434745894'],
            'region_code': 'us',
            'record_label': 'UMGRI Interscope',
            'duration': 245.987,
            'genre_ids': ['20', '34'],
        },
        'params': {
            'extractor_args': {'applemusic': {
                'max_thumbnail_width': ['1234'],
                'max_thumbnail_height': ['4321'],
            }},
            'skip_download': True,
            'ignore_no_formats_error': True,
        },
        'expected_warnings': [
            'Song is unplayable',
            'No video formats found',
            'Requested format is not available',
        ],
    }, {
        'note': 'only available in lossy',
        'url': 'https://music.apple.com/us/album/heart-no-3/1216647292?i=1216648056',
        'info_dict': {
            'id': '1216648056',
            'is_apple_digital_master': False,
            'genre_ids': ['34', '10'],
            'disc_number': 1,
            'album': 'I\'m Becoming Part Crow',
            'storefront_id': 143441,
            'artists': ['Daniel M. P. Shaw'],
            'release_date': '20170314',
            'composers': ['Daniel Mark Paget Shaw'],
            'duration': 88.625,
            'title': 'Heart No. 3',
            'album_id': '1216647292',
            'genres': ['Alternative Folk', 'Music', 'Singer/Songwriter'],
            'thumbnail': r're:^https://.+\.mzstatic\.com/image/thumb/.+\.jpg',
            'track_count': 13,
            'album_artists': ['Daniel M. P. Shaw'],
            'record_label': 'Boj River Music',
            'upc': '191061467359',
            'track_id': '1216648056',
            'copyright': '℗ 2017 Daniel M. P. Shaw',
            'track': 'Heart No. 3',
            'region_code': 'us',
            'track_number': 1,
            'artist_ids': ['1211880898'],
            'media_type': 'song',
            'isrc': 'CHC991700022',
        },
        'params': {
            'skip_download': True,
            'ignore_no_formats_error': True,
        },
        'expected_warnings': [
            'Song is not available over HLS',
            'No video formats found',
            'Requested format is not available',
        ],
    }, {
        'url': 'https://music.apple.com/ca/song/the-shortest-straw/1433828083',
        'only_matching': True,
    }]

    def _extract_lyrics(self, region, song_id):
        if not self.get_param('http_headers').get('Media-User-Token'):
            self.to_screen('No Media-User-Token provided, skipping lyrics extraction')
            return []
        subtitles = {}
        # will return 404 if:
        # * Media-User-Token is not present or is invalid (?)
        # * the song doesn't have lyrics
        # * current storefront and the account's region setting mismatch
        for endpoint in ('lyrics', 'syllable-lyrics'):
            if ttml_data := traverse_obj(self._download_api_json(
                f'https://amp-api.music.apple.com/v1/catalog/{region}/songs/{song_id}/{endpoint}',
                    video_id=song_id, expected_status=404, note=f'Downloading {endpoint}'),
                    ('data', ..., 'attributes', 'ttml', any)):
                subtitles[endpoint] = [{'data': ttml_data, 'ext': 'ttml'}]
        return subtitles

    def _extract_formats(self, song, song_id):
        # Unfortunately, due to the extreme complexity of '_parse_m3u8_formats_and_subtitles',
        # it's impossible to extract some useful info (such as channel, asr and whether the
        # track is Dolby Atmos or not) wthout re-implementing the entire method
        assets = traverse_obj(song, ('attributes', 'extendedAssetUrls', {dict}))
        if not assets:
            self.raise_no_formats('Song is unplayable', expected=True, video_id=song_id)
        elif hls := traverse_obj(assets, ('enhancedHls', {url_or_none})):
            return self._extract_m3u8_formats(hls, video_id=song_id, headers=self._SUPPRESS_AUTH)
        else:
            self.raise_no_formats('Song is not available over HLS', expected=True, video_id=song_id)
        return []

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        region = mobj.group('region')
        song_id = mobj.group('song_id') or mobj.group('song_id_2')

        resp = self._download_api_json(
            f'https://amp-api.music.apple.com/v1/catalog/{region}/songs/{song_id}?extend=extendedAssetUrls&include=albums,genres',
            video_id=song_id, headers=self._SUPPRESS_USER_AUTH, query=self._get_lang_query(url), fatal=False)
        if not resp:
            raise ExtractorError(
                'This song either does not exist or is unavailable in the current region', expected=True)
        song = traverse_obj(resp, ('data', ..., any))

        metadata = {
            'id': song_id,
            'formats': self._extract_formats(song, song_id),
            **self._extract_common_metadata(song),
            **self._extract_album_metadata(traverse_obj(song, ('relationships', 'albums', 'data', 0, {dict}))),
            **traverse_obj(song, ('attributes', {
                'album': ('albumName', {str}),
                'composer': ('composerName', {str}),  # XXX: deprecated by yt-dlp
                # 'composers': ('composerName', {variadic}, {lambda x: x[0] and list(x)}),
                'track': ('name', {str}),
                'track_number': ('trackNumber', {int_or_none}),
                'track_id': ('playParams', 'id', {str}),
                'media_type': ('playParams', 'kind', {str}),
                'disc_number': ('discNumber', {int_or_none}),
                'duration': ('durationInMillis', {int_or_none}, {lambda x: x / 1000}),
                # not part of yt-dlp
                'isrc': ('isrc', {str}),
            })),
            'thumbnails': [self._extract_thumbnail(traverse_obj(song, ('attributes', 'artwork')))],
            'subtitles': (self._extract_lyrics(region, song_id)
                          if traverse_obj(song, ('attributes', 'hasLyrics', {bool})) else []),
            'http_headers': self._SUPPRESS_AUTH,
            # not part of yt-dlp
            **traverse_obj(song, ('relationships', {
                'artist_ids': ('artists', 'data', ..., 'id', {str}, all),
                'genre_ids': ('genres', 'data', ..., 'id', {str}, all),
            })),
            'region_code': region,
        }
        if sfid := _STOREFRONT_ID_MAP.get(region.upper()):
            metadata['storefront_id'] = sfid
        else:
            self.report_warning(f'Unrecognized region code "{region}"')

        return metadata


class AppleMusicAlbumIE(AppleMusicBaseIE):
    _VALID_URL = AppleMusicBaseIE._VALID_URL_BASE + \
        r'(?P<region>[a-z]{2})/album/.+/(?P<album_id>[0-9]+)(?:(?!(?:\?|&)i=[0-9]+).)*$'
    _TESTS = [{
        'url': 'https://music.apple.com/us/album/joyride/1754468855',
        'info_dict': {
            'id': '1754468855',
            'title': 'JOYRIDE - Single',
            'release_date': '20240704',
            'age_limit': 18,
            'genres': ['Pop', 'Music'],
            'is_apple_digital_master': False,
            'album_id': '1754468855',
            'upc': '8721093407898',
            'record_label': 'Kesha Records',
            'track_count': 1,
            'copyright': '℗ 2024 Kesha Records',
            'thumbnails': 'count:1',
            'album_type': 'Single',
            'album_artists': ['Kesha'],
            'artists': ['Kesha'],
        },
        'playlist_count': 1,
    }, {
        'note': 'album with unavailable tracks',
        'url': 'https://music.apple.com/us/album/me-before-you-original-motion-picture-soundtrack/1440843089',
        'info_dict': {
            'title': 'Me Before You (Original Motion Picture Soundtrack)',
            'is_apple_digital_master': True,
            'record_label': 'UMGRI Interscope',
            'album_id': '1440843089',
            'album_artists': ['Various Artists'],
            'release_date': '20160603',
            'genres': ['Soundtrack', 'Music'],
            'album_type': 'Compilation',
            'upc': '00602547938022',
            'artists': ['Various Artists'],
            'track_count': 9,
            'id': '1440843089',
            'copyright': 'This Compilation ℗ 2016 Interscope Records',
        },
        'playlist_count': 9,
        'params': {'flat_playlist': True},
    }, {
        'note': 'only animated cover',
        'url': 'https://music.apple.com/ca/album/a-head-full-of-dreams/1053933969',
        'info_dict': {
            'id': '1053933969',
            'ext': 'mp4',
            'title': 'A Head Full of Dreams',
            'record_label': 'Parlophone UK',
            'genres': ['Alternative', 'Music', 'Rock', 'Adult Alternative', 'Pop', 'Britpop'],
            'copyright': '℗ 2015 Parlophone Records Limited, a Warner Music Group Company',
            'track_count': 11,
            'thumbnail': r're:^https://.+\.mzstatic\.com/image/thumb/.+\.jpg',
            'album_artists': ['Coldplay'],
            'release_date': '20151204',
            'upc': '190295998783',
            'album_id': '1053933969',
            'artists': ['Coldplay'],
            'description': 'md5:78184cb419e150d4050f803e6233ad51',
            'media_type': 'editorialVideo',
            'is_apple_digital_master': True,
        },
        'params': {
            'noplaylist': True,
            'skip_download': True,
        }
    }, {
        'note': 'animated cover and album',
        'url': 'https://music.apple.com/ca/album/music-of-the-spheres/1576349937',
        'info_dict': {
            'id': '1576349937',
            'genres': ['Pop', 'Music'],
            'track_count': 12,
            'record_label': 'Parlophone UK',
            'album_artists': ['Coldplay'],
            'description': 'md5:fbd9c509643265791ac65d169691cdec',
            'is_apple_digital_master': True,
            'album_id': '1576349937',
            'copyright': 'Under exclusive licence to Parlophone Records Limited, ℗ 2021 Coldplay',
            'id': '1576349937',
            'artists': ['Coldplay'],
            'release_date': '20211015',
            'title': 'Music of the Spheres',
            'age_limit': 18,
            'upc': '190296529818',
        },
        'playlist_count': 13,
        'params': {
            'skip_download': True,
            'flat_playlist': True,
        },
    }, {
        'note': 'album without an animated cover',
        'url': 'https://music.apple.com/us/album/tgif/1752805219',
        'info_dict': {
            'id': '1752805219',
            'title': 'TGIF - Single',
            'record_label': 'CMG/Interscope Records',
            'age_limit': 18,
            'upc': '00602465973747',
            'album_artists': ['GloRilla'],
            'track_count': 1,
            'copyright': '℗ 2024 CMG/Interscope Records',
            'artists': ['GloRilla'],
            'is_apple_digital_master': True,
            'album_type': 'Single',
            'album_id': '1752805219',
            'release_date': '20240621',
            'genres': ['Hip-Hop/Rap', 'Music'],
        },
        'params': {
            'noplaylist': True,
            'ignore_no_formats_error': True,
            'skip_download': True,
        },
        'expected_warnings': [
            'This album does not have an animated cover',
            'No video formats found',
            'Requested format is not available',
        ],
    }, {
        'note': 'region-locked album',
        'url': 'https://music.apple.com/jp/album/mylo-xyloto/693580048',
        'info_dict': {
            'id': '693580048',
            'album_id': '693580048',
            'copyright': '℗ 2011 Parlophone Records Ltd, a Warner Music Group Company',
            'release_date': '20111019',
            'artists': ['コールドプレイ'],
            'genres': ['オルタナティブ', 'ミュージック', 'ロック', 'アダルト・アルタナティブ'],
            'upc': '5099972943854',
            'album_artists': ['コールドプレイ'],
            'is_apple_digital_master': True,
            'record_label': 'Parlophone UK',
            'title': 'Mylo Xyloto',
            'track_count': 17,
            'description': 'md5:62eeb52901a4ec8142dfb1d2b83be61b',
        },
        'playlist_count': 17,
        'params': {
            'skip_download': True,
            'flat_playlist': True,
        },
    }, {
        'note': 'different language code',
        'url': 'https://music.apple.com/jp/album/mylo-xyloto/693580048?l=en-US',
        'info_dict': {
            'id': '693580048',
            'is_apple_digital_master': True,
            'genres': ['Alternative', 'Music', 'Rock', 'Adult Alternative'],
            'release_date': '20111019',
            'album_id': '693580048',
            'track_count': 17,
            'upc': '5099972943854',
            'description': 'md5:7ce74b19a24516d679ccaa5db0b1611f',
            'title': 'Mylo Xyloto',
            'album_artists': ['Coldplay'],
            'record_label': 'Parlophone UK',
            'artists': ['Coldplay'],
            'copyright': '℗ 2011 Parlophone Records Ltd, a Warner Music Group Company',
        },
        'playlist_count': 17,
        'params': {
            'skip_download': True,
            'flat_playlist': True,
        },
    }, {
        'url': 'https://geo.music.apple.com/us/album/_/1752805219',
        'only_matching': True,
    }, {
        'url': 'https://beta.music.apple.com/us/album/tgif/1752805219',
        'only_matching': True,
    }]

    def _extract_animated_cover(self, album, video_id):
        formats = []
        thumbnails = []

        seen = set()  # remove duplicate URLs
        for name, data in traverse_obj(
            album, ('attributes', 'editorialVideo', {dict.items},
                    lambda _, v: not (v[1]['video'] in seen or seen.add(v[1]['video'])))):
            formats.extend(self._extract_m3u8_formats(
                data.get('video'), video_id=video_id, m3u8_id=name, headers=self._SUPPRESS_AUTH))
            thumbnails.append(self._extract_thumbnail(data.get('previewFrame')))

        if not formats:
            return {}

        for f in formats:
            f['url'] = re.sub(r'-?\.m3u8', '-.mp4', f['url'])
            f['protocol'] = 'http'

        return {
            'id': video_id,
            'formats': formats,
            'thumbnails': thumbnails,
            'media_type': 'editorialVideo',
        }

    def _yes_playlist(self, *args, **kwargs):
        return super()._yes_playlist(
            True, True, playlist_label='both the animated cover and the album',
            video_label='animated album cover')

    def _real_extract(self, url):
        region, album_id = self._match_valid_url(url).groups()

        resp = self._download_api_json(
            f'https://amp-api.music.apple.com/v1/catalog/{region}/albums/{album_id}?include=tracks&extend=editorialVideo',
            video_id=album_id, headers=self._SUPPRESS_USER_AUTH, query=self._get_lang_query(url), fatal=False)
        if not resp:
            raise ExtractorError(
                'This album either does not exist or is unavailable in the current region', expected=True)
        album = traverse_obj(resp, ('data', ..., any))

        metadata = {
            **self._extract_common_metadata(album),
            **self._extract_album_metadata(album),
        }

        if cover_data := self._extract_animated_cover(album, video_id=album_id):
            animated_cover = {
                **metadata,
                **cover_data,
                'http_headers': self._SUPPRESS_AUTH,
            }
            if not self._yes_playlist():
                return animated_cover

            entries = [animated_cover]
        elif self._yes_playlist():
            self.to_screen('This album does not have an animated cover')
            entries = []

        else:
            self.raise_no_formats(
                'This album does not have an animated cover', expected=True, video_id=album_id)
            return {
                'id': album_id,
                **metadata,
                'formats': [],
                'http_headers': self._SUPPRESS_AUTH,
            }

        entries.extend(
            self.url_result(song_url, AppleMusicIE) for song_url in
            traverse_obj(album, ('relationships', 'tracks', 'data', ..., 'attributes', 'url', {url_or_none})))

        return {
            '_type': 'playlist',
            'entries': entries,
            'id': album_id,
            **metadata,
            'thumbnails': [self._extract_thumbnail(traverse_obj(album, ('attributes', 'artwork')))],
        }


# extracted from https://music.apple.com/includes/js-cdn/musickit/v3/amp/musickit.js
_STOREFRONT_ID_MAP = {
    'AF': 143610,
    'AO': 143564,
    'AI': 143538,
    'AL': 143575,
    'AD': 143611,
    'AE': 143481,
    'AR': 143505,
    'AM': 143524,
    'AG': 143540,
    'AU': 143460,
    'AT': 143445,
    'AZ': 143568,
    'BE': 143446,
    'BJ': 143576,
    'BF': 143578,
    'BD': 143490,
    'BG': 143526,
    'BH': 143559,
    'BS': 143539,
    'BA': 143612,
    'BY': 143565,
    'BZ': 143555,
    'BM': 143542,
    'BO': 143556,
    'BR': 143503,
    'BB': 143541,
    'BN': 143560,
    'BT': 143577,
    'BW': 143525,
    'CF': 143623,
    'CA': 143455,
    'CH': 143459,
    'CL': 143483,
    'CN': 143465,
    'CI': 143527,
    'CM': 143574,
    'CD': 143613,
    'CG': 143582,
    'CO': 143501,
    'CV': 143580,
    'CR': 143495,
    'KY': 143544,
    'CY': 143557,
    'CZ': 143489,
    'DE': 143443,
    'DM': 143545,
    'DK': 143458,
    'DO': 143508,
    'DZ': 143563,
    'EC': 143509,
    'EG': 143516,
    'ES': 143454,
    'EE': 143518,
    'ET': 143569,
    'FI': 143447,
    'FJ': 143583,
    'FR': 143442,
    'FM': 143591,
    'GA': 143614,
    'GB': 143444,
    'GE': 143615,
    'GH': 143573,
    'GN': 143616,
    'GM': 143584,
    'GW': 143585,
    'GR': 143448,
    'GD': 143546,
    'GT': 143504,
    'GY': 143553,
    'HK': 143463,
    'HN': 143510,
    'HR': 143494,
    'HU': 143482,
    'ID': 143476,
    'IN': 143467,
    'IE': 143449,
    'IQ': 143617,
    'IS': 143558,
    'IL': 143491,
    'IT': 143450,
    'JM': 143511,
    'JO': 143528,
    'JP': 143462,
    'KZ': 143517,
    'KE': 143529,
    'KG': 143586,
    'KH': 143579,
    'KN': 143548,
    'KR': 143466,
    'KW': 143493,
    'LA': 143587,
    'LB': 143497,
    'LR': 143588,
    'LY': 143567,
    'LC': 143549,
    'LI': 143522,
    'LK': 143486,
    'LT': 143520,
    'LU': 143451,
    'LV': 143519,
    'MO': 143515,
    'MA': 143620,
    'MC': 143618,
    'MD': 143523,
    'MG': 143531,
    'MV': 143488,
    'MX': 143468,
    'MK': 143530,
    'ML': 143532,
    'MT': 143521,
    'MM': 143570,
    'ME': 143619,
    'MN': 143592,
    'MZ': 143593,
    'MR': 143590,
    'MS': 143547,
    'MU': 143533,
    'MW': 143589,
    'MY': 143473,
    'NA': 143594,
    'NE': 143534,
    'NG': 143561,
    'NI': 143512,
    'NL': 143452,
    'NO': 143457,
    'NP': 143484,
    'NR': 143606,
    'NZ': 143461,
    'OM': 143562,
    'PK': 143477,
    'PA': 143485,
    'PE': 143507,
    'PH': 143474,
    'PW': 143595,
    'PG': 143597,
    'PL': 143478,
    'PT': 143453,
    'PY': 143513,
    'PS': 143596,
    'QA': 143498,
    'RO': 143487,
    'RU': 143469,
    'RW': 143621,
    'SA': 143479,
    'SN': 143535,
    'SG': 143464,
    'SB': 143601,
    'SL': 143600,
    'SV': 143506,
    'RS': 143500,
    'ST': 143598,
    'SR': 143554,
    'SK': 143496,
    'SI': 143499,
    'SE': 143456,
    'SZ': 143602,
    'SC': 143599,
    'TC': 143552,
    'TD': 143581,
    'TH': 143475,
    'TJ': 143603,
    'TM': 143604,
    'TO': 143608,
    'TT': 143551,
    'TN': 143536,
    'TR': 143480,
    'TW': 143470,
    'TZ': 143572,
    'UG': 143537,
    'UA': 143492,
    'UY': 143514,
    'US': 143441,
    'UZ': 143566,
    'VC': 143550,
    'VE': 143502,
    'VG': 143543,
    'VN': 143471,
    'VU': 143609,
    'WS': 143607,
    'XK': 143624,
    'YE': 143571,
    'ZA': 143472,
    'ZM': 143622,
    'ZW': 143605,
}

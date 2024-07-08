import functools
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    NUMBER_RE,
    dfxp2srt,
    int_or_none,
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
    _VALID_URL_BASE = r'^https?://(?:(?:geo|beta)\.)?music\.apple\.com/(?P<region>[a-z]{2})/album/.+/(?P<album_id>[0-9]+)'

    _SUPPRESS_AUTH = {'Authorization': '', 'Media-User-Token': ''}
    _SUPPRESS_USER_AUTH = {'Media-User-Token': ''}
    _api_headers = {'Origin': 'https://music.apple.com'}

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

        if self._api_headers.get('Authorization'):
            result, urlh = self._download_json_handle(
                *args, expected_status=codes, headers={**self._api_headers, **headers}, **kwargs)
            if urlh.status != 401:
                return result
        elif cached_token := self.cache.load('applemusic', 'token'):
            self._api_headers['Authorization'] = f'Bearer {cached_token}'
            result, urlh = self._download_json_handle(
                *args, expected_status=codes, headers={**self._api_headers, **headers}, **kwargs)
            if urlh.status != 401:
                return result

        # anonymous token has expired or is not cached
        token = self._get_anonymous_token(video_id=kwargs.get('video_id'))
        self.cache.store('applemusic', 'token', token)
        self._api_headers['Authorization'] = f'Bearer {token}'

        # should not return 401
        return self._download_json(*args, expected_status=expected_status,
                                   headers={**self._api_headers, **headers}, **kwargs)

    def _extract_thumbnail(self, obj):
        return traverse_obj(obj, ('attributes', 'artwork', {
            'url': ('url', {str}, {lambda x: x.replace(
                    '{w}x{h}', f'{self._MAX_THUMBNAIL_WIDTH}x{self._MAX_THUMBNAIL_HEIGHT}')}),
            # XXX: remove these if max width/height is specified?
            'width': ('width', {int_or_none}),
            'height': ('height', {int_or_none}),
        }))

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
            # not necessarily equals to the number of tracks returned, since
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
    _VALID_URL = AppleMusicBaseIE._VALID_URL_BASE + r'.*(?:\?|&)i=(?P<song_id>[0-9]+)'

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

    def _real_extract(self, url):
        region, _, song_id = self._match_valid_url(url).groups()
        resp = self._download_api_json(
            f'https://amp-api.music.apple.com/v1/catalog/{region}/songs/{song_id}?extend=extendedAssetUrls&include=albums,genres',
            video_id=song_id, headers=self._SUPPRESS_USER_AUTH,
            query=self._get_lang_query(url))
        song = traverse_obj(resp, ('data', ..., any))

        formats = self._extract_m3u8_formats(
            traverse_obj(song, ('attributes', 'extendedAssetUrls', 'enhancedHls')),
            video_id=song_id, headers=self._SUPPRESS_AUTH)
        # workaround for yt-dlp's format parser not recognizing 'alac'
        # (the selector does recognize it)
        for f in formats:
            if not f.get('acodec') and 'alac' in (f.get('url') or ''):
                f['acodec'] = 'alac'
                f['vcodec'] = f['video_ext'] = 'none'
                f['ext'] = f['audio_ext'] = 'm4a'

        metadata = {
            'id': song_id,
            **self._extract_common_metadata(song),
            **self._extract_album_metadata(traverse_obj(song, ('relationships', 'albums', 'data', 0, {dict}))),
            **traverse_obj(song, ('attributes', {
                'album': ('albumName', {str}),
                'composer': ('composerName', {str}),  # XXX: deprecated by yt-dlp
                # 'composers': ('composerName', {variadic}, {lambda x: x[0] and list(x)}),
                'track': ('name', {str}),
                'track_number': ('trackNumber', {int_or_none}),
                'track_id': ('playParams', 'id', {str}),
                'disc_number': ('discNumber', {int_or_none}),
                'duration': ('durationInMillis', {int_or_none}, {lambda x: x / 1000}),
                # not part of yt-dlp
                'isrc': ('isrc', {str}),
            })),
            'formats': formats,
            'thumbnails': [self._extract_thumbnail(song)],
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
    _VALID_URL = AppleMusicBaseIE._VALID_URL_BASE + r'(?:(?!(?:\?|&)i=[0-9]+).)*$'

    def _real_extract(self, url):
        region, album_id = self._match_valid_url(url).groups()

        resp = self._download_api_json(
            f'https://amp-api.music.apple.com/v1/catalog/{region}/albums/{album_id}?include=tracks',
            video_id=album_id, headers=self._SUPPRESS_USER_AUTH, query=self._get_lang_query(url))
        album = traverse_obj(resp, ('data', ..., any))

        return {
            '_type': 'playlist',
            'entries': [self.url_result(song_url, AppleMusicIE) for song_url in
                        traverse_obj(album, ('relationships', 'tracks', 'data', ..., 'attributes', 'url', {url_or_none}))],
            'id': album_id,
            **self._extract_common_metadata(album),
            **self._extract_album_metadata(album),
            'thumbnails': [self._extract_thumbnail(album)],
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

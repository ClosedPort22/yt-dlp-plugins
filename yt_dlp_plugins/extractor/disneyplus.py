import re
import uuid
import json
import functools
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import traverse_obj, int_or_none, unified_strdate, parse_age_limit


from yt_dlp import YoutubeDL
_old_urlopen = YoutubeDL.urlopen


def _urlopen_patched(obj, req):
    # ignore HLS discontinuities
    resp = _old_urlopen(obj, req)
    url = resp.url
    if '.m3u8' not in url:
        return resp

    def read():
        def readlines(response):
            for line in response.fp:
                if b"DISCONTINUITY" in line:
                    obj.report_warning(
                        'Initialization fragment found after media fragments, '
                        'ignoring the rest of the playlist')
                    response.close()
                    return
                yield line
        return b''.join(readlines(resp))
    resp.read = read
    return resp


YoutubeDL.urlopen = _urlopen_patched


class DisneyPlusBaseIE(InfoExtractor):
    _VALID_URL = False  # not sure why this is necessary

    @functools.cached_property
    def _REGION(self):
        return self._configuration_arg('region', ['US'], ie_key=DisneyPlusIE)[0]

    @functools.cached_property
    def _LANGUAGE(self):
        return self._configuration_arg('language', ['en'], ie_key=DisneyPlusIE)[0]

    def _download_bamgrid_json(self, *args, **kwargs):
        # TODO: support for cookies or even logging in?
        # auth required
        kwargs.setdefault('expected_status', 401)
        result, urlh = self._download_json_handle(*args, **kwargs)
        if urlh.status != 401:
            return result
        description = traverse_obj(result, ('errors', ..., 'description'), get_all=False)
        self.raise_login_required({
            'auth.expired': 'Authentication token expired',
            'auth.missing': 'No authentication token provided',
            'auth.malformed': 'Malformed authentication token',
        }.get(description, f'Disney+ says: {description}'))
        return result

    def _download_dmc_json(self, endpoint, path, *args, **kwargs):
        # no auth required
        url = (f'https://disney.content.edge.bamgrid.com/svc/content/{endpoint}/'
               f'version/5.1/region/{self._REGION}/audience/false/maturity/1899/language/{self._LANGUAGE}{path}')
        return self._download_json(url, *args, **kwargs)


class DisneyPlusIE(DisneyPlusBaseIE):
    _VALID_URL = (r'^https?://(?:www\.)?disneyplus\.com/play/'
                  r'(?P<id>[0-9a-f]{8}\b(?:-[0-9a-f]{4}\b){3}-[0-9a-f]{12})')
    _BAMSDK_HEADERS = {
        'Accept': 'application/vnd.media-service+json',
        'Content-Type': 'application/json',
        'X-Dss-Edge-Accept': 'vnd.dss.edge+json; version=2',
        'X-BAMSDK-VERSION': '28.4',
        'X-Bamsdk-Client-Id': 'disney-svod-3d9324fc',
        'X-BAMSDK-PLATFORM': 'javascript/windows/chrome',
        'X-Dss-Feature-Filtering': 'true',
        'X-Application-Version': '1.1.2',
    }

    def _real_extract(self, url):
        video_id = self._match_id(url)
        headers = {
            'Accept': 'application/json',
            'Referer': 'https://www.disneyplus.com/'
        }
        video_data = self._download_bamgrid_json(
            f'https://disney.api.edge.bamgrid.com/explore/v1.4/deeplink?action=playback&refId={video_id}&refIdType=deeplinkId',
            video_id=video_id, headers=headers)
        resource_id = traverse_obj(video_data, ('data', 'deeplink', 'actions', ..., 'resourceId'), get_all=False)
        content_id = traverse_obj(
            video_data, ('data', 'deeplink', 'actions', ..., 'partnerFeed', 'dmcContentId'), get_all=False)
        data = {
            "playback": {
                "attributes": {
                    "codecs": {
                        "video": ["h.264", "h.265"],
                        "supportsMultiCodecMaster": True,
                    },
                    "protocol": "HTTPS",
                    "videoRanges": ["DOLBY_VISION"],
                    "assetInsertionStrategy": "SGAI",
                    "playbackInitiationContext": "ONLINE",
                    "frameRates": [60],
                    "slugDuration": "SLUG_500_MS"
                },
                "adTracking": {
                    "limitAdTrackingEnabled": "YES",
                    "deviceAdId": "00000000-0000-0000-0000-000000000000"
                },
                "tracking": {"playbackSessionId": str(uuid.uuid4())}
            },
            "playbackId": resource_id,
        }
        scenario = self._configuration_arg('playback_scenario', ['ctr-regular'])[0]
        playback = self._download_bamgrid_json(
            f'https://disney.playback.edge.bamgrid.com/v7/playback/{scenario}',
            video_id=video_id, headers=self._BAMSDK_HEADERS, data=json.dumps(data).encode())

        formats = []
        subtitles = []
        for source in traverse_obj(playback, ('stream', 'sources', ..., {
            'priority': ('priority', {int_or_none}),
            'url': ('complete', 'url', {str}),
            'id': ('complete', 'tracking', 'telemetry', 'cdn', {str}),
        })):
            fmts, subtitles = self._extract_m3u8_formats_and_subtitles(
                source['url'], video_id=video_id, preference=-(source['priority'] or 0), m3u8_id=source['id'],
                headers={'Authorization': ''})
            formats.extend(fmts)

        # parse chapters
        milestones = {}
        chapters = []
        for milestone in traverse_obj(playback, ('stream', 'editorial')) or ():
            milestones[traverse_obj(milestone, ('label', {str}))] = \
                traverse_obj(milestone, ('offsetMillis', {int_or_none}))

        if end_millis := traverse_obj(milestones, 'intro_end', 'LFEI'):
            start_millis = traverse_obj(milestones, 'intro_start', 'FFEI') or 0
            chapters.append({
                'title': 'Intro',
                'start_time': int(round(start_millis / 1000)),
                'end_time': int(round(end_millis / 1000)),
            })
        if credits := milestones.get('FFEC'):
            chapters.append({
                'title': 'Credits',
                'start_time': int(round(credits / 1000)),
            })

        bitrate_re = re.compile(r'r/composite_(\d+)k')
        for fmt in formats:
            if fmt.get('vcodec') != 'none':
                continue
            # audio tracks do not have DRM
            fmt['has_drm'] = False
            if not fmt.get('abr') and (mobj := bitrate_re.search(fmt['url'])):
                fmt['abr'] = int(mobj.group(1))
            if fmt.get('acodec'):
                continue
            if 'eac-3-' in fmt['format_id']:
                fmt['acodec'] = 'eac3'
            elif 'aac-' in fmt['format_id']:
                fmt['acodec'] = 'aac'

        # returns null if region code is incorrect
        dmc_data = self._download_dmc_json('DmcVideo', '/contentId/' + content_id, video_id=video_id)

        extracted_data = traverse_obj(dmc_data, ('data', 'DmcVideo', 'video', {
            'title': ('text', 'title', 'full', 'program', 'default', 'content', {str}),
            'series_id': ('seriesId', {str}),
            'series': ('text', 'title', 'full', 'series', 'default', 'content', {str}),
            'season_number': ('seasonSequenceNumber', {int_or_none}),
            'season_id': ('seasonId', {str}),
            'season': ('text', 'title', 'full', 'season', 'default', 'content', {str}),
            'episode_number': ('episodeNumber', 'episodeSequenceNumber', {int_or_none}),
            'description': ('text', 'description', ('full', 'medium', 'brief'), 'program', 'default', 'content', {str}, any),
            'creators': ('participant', 'Creator', ..., 'displayName', {str}),
            'age_limit': ('ratings', ..., 'value', {parse_age_limit}, any),
            'release_date': ('releases', ..., 'releaseDate', {unified_strdate}, any),
            'thumbnails': ('image', 'thumbnail', {dict.values}, ..., 'program', 'default', {
                'width': ('masterWidth', {int_or_none}),
                'height': ('masterHeight', {int_or_none}),
                'url': ('url', {str}),
            }),
        }))

        if traverse_obj(extracted_data, ('data', 'DmcVideo', 'video', 'programType')) == 'episode' and \
                extracted_data.get('title'):
            extracted_data['episode'] = extracted_data['title']

        extracted_data.update({
            'id': video_id,
            'subtitles': subtitles,
            'formats': formats,
            'chapters': chapters,
            'http_headers': {'Authorization': ''},
        })
        return extracted_data

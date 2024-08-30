import re
import urllib.parse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import parse_age_limit, parse_qs, traverse_obj, unified_strdate, url_or_none


class ItunesHLSIE(InfoExtractor):
    IE_NAME = 'itunes:hls'
    _VALID_URL = r'^https?://play\.itunes\.apple\.com/WebObjects/MZPlay\.woa/hls/playlist\.m3u8(?:\?|.+&)a=(?P<id>\d+)'
    _TESTS = [{
        'url': 'https://play.itunes.apple.com/WebObjects/MZPlay.woa/hls/playlist.m3u8?cc=GB&a=965491522&id=236366768&l=en&aec=SD',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        qs = parse_qs(url)
        qs.pop('dsid', None)  # dsid is linked to the user's account

        m3u8_doc = self._download_webpage(
            'https://play.itunes.apple.com/WebObjects/MZPlay.woa/hls/playlist.m3u8',
            video_id, query=qs, note='Downloading m3u8 information', errnote='Failed to download m3u8 information')

        metadata = {}
        for mobj in re.finditer(r'(?m)^#EXT-X-SESSION-DATA:(?P<attributes>.+)', m3u8_doc):
            attributes = mobj.group("attributes").split(",")
            data = {k: v.strip('"') for k, _, v in (attr.partition('=') for attr in attributes)}
            metadata[data.get("DATA-ID")] = data.get('VALUE')

        url = f'https://play.itunes.apple.com/WebObjects/MZPlay.woa/hls/playlist.m3u8?{urllib.parse.urlencode(qs)}'
        formats, subtitles = self._parse_m3u8_formats_and_subtitles(
            m3u8_doc=m3u8_doc, ext='mp4', m3u8_url=url, video_id=video_id)

        for f in formats:
            f["has_drm"] = True

        return {
            'id': video_id,
            **traverse_obj(metadata, {
                'series': 'com.apple.hls.title',
                'title': 'com.apple.hls.episode-title',
                'episode': 'com.apple.hls.episode-title',
                'description': 'com.apple.hls.description',
                'age_limit': ('com.apple.hls.rating-tag', {parse_age_limit}),
                'release_date': ('com.apple.hls.release-date', {unified_strdate}),
                'thumbnail': ('com.apple.hls.poster', {url_or_none}, {lambda x: x.format(w=12000, h=12000, f='jpg')}),
            }),
            'formats': formats,
            'subtitles': subtitles,
        }

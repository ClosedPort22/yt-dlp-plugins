import json

from yt_dlp.extractor.common import InfoExtractor, int_or_none, mimetype2ext, traverse_obj, unified_strdate, url_or_none


class ABCListenAudiobookBaseIE(InfoExtractor):
    _VALID_URL = False
    # _GEO_COUNTRIES = ['AU']  # API is not geo-blocked, but media URLs are, and spoofing XFF doesn't work for them

    _OPERATION_HASHES = {
        'GetProgramDetails': 'cf85aaeb21166618a73cf656e6ac2bf08f84bf931a6025d5d47da2f664753fb0',
        'GetEpisodeById': '23b71c3423bb8dfe3fa65e3533c92d16bb13a9953f0fe868186a0d41baf1ae91',
        # 'GetEpisodeDetails': 'd5e6344c3c2bdedfc27e17903f28c402972f73dfc2ac9bf26ee3a59220333342',  # not enough metadata
    }
    _API_KEY = 'lisf49553d5c441652e95697a2c5949f'
    _USER_AGENT = 'ABC listen/2024.11.5197 (5197)/Android 33'

    def _call_graphql_api(self, video_id, operation, variables, **kwargs):
        operation_hash = self._OPERATION_HASHES[operation]
        return self._download_json(
            'https://api.abc.net.au/terminus/graphql/query', video_id, query={
                'operationName': operation,
                'variables': json.dumps(variables),
                'extensions': json.dumps({
                    'persistedQuery': {
                        'version': 1,
                        'sha256Hash': operation_hash,
                    },
                }),
            }, headers={
                'Accept': 'multipart/mixed; deferSpec=20220824, application/json',
                'X-Api-Key': self._API_KEY,
                'X-APOLLO-OPERATION-ID': operation_hash,
                'X-APOLLO-OPERATION-NAME': operation,
                'User-Agent': self._USER_AGENT,
            }, **kwargs)['data']


class ABCListenAudiobookIE(ABCListenAudiobookBaseIE):
    IE_NAME = 'abc.net.au:listen:audiobook'
    _VALID_URL = r'https?://(?:www\.)?abc\.net\.au/listen/audiobooks/(?P<id>[^/]+)/?$'

    _TESTS = [{
        'url': 'https://www.abc.net.au/listen/audiobooks/488-rules-for-life-the-thankless-art-of-being-correct/',
        'info_dict': {
            'id': '13391656',
            'description': 'md5:380bf895bc390c1c14871035decd2444',
            'title': '488 Rules for Life: The thankless art of being correct',
            'thumbnails': 'count:2',
        },
        'playlist_count': 92,
        'params': {'skip_download': True},
    }, {
        'url': 'https://www.abc.net.au/listen/audiobooks/dreams-from-my-father',
        'info_dict': {
            'id': '104315952',
            'description': 'md5:67f30b8de88ff7aa3a215fff5ce5eb71',
            'title': 'Dreams From My Father: A Story of Race and Inheritance',
            'thumbnails': 'count:2',
        },
        'playlist': [{
            'info_dict': {
                'id': '104315946',
                'ext': 'mp3',
                'title': 'Preface',
                'description': 'md5:67f30b8de88ff7aa3a215fff5ce5eb71',
                'duration': 718,
                'release_date': '20241027',
            },
        }],
        'params': {
            'playlist_items': '1',
            'skip_download': True,
        },
    }]

    def _real_extract(self, url):
        slug = self._match_id(url)
        video_id = self._search_regex(
            r'coremedia://program/(\d+)', self._download_webpage(url, slug), 'audiobook ID', fatal=True)
        # FIXME: handle audiobooks with more than 250 episodes?
        program = self._call_graphql_api(
            video_id, 'GetProgramDetails', {'id': video_id, 'episodeLimit': 250})['program']
        return {
            '_type': 'playlist',
            'id': video_id,
            **traverse_obj(program, {
                'title': (('title', 'teaserTitle', 'shortTeaserTitle', 'sortTitle'), {str}, any),
                'description': ('description', 'plainText', {str}),
                'entries': ('programContentCollection', 'document', ..., 'items', ..., {
                    'id': 'id',
                    'title': (('title', 'teaserTitle', 'shortTeaserTitle', 'sortTitle'), {str}, any),
                    'release_date': ('publicationDate', {unified_strdate}),
                    'description': ('caption', 'plainText', {str}),
                    'duration': ('duration', {int_or_none}),
                    'formats': ('renditions', ..., {
                        'vcodec': {lambda _: 'none'},
                        'ext': ('contentType', {mimetype2ext}),
                        'url': ('url', {url_or_none}),
                    }, all),
                }),
            }),
            'thumbnails': [
                {'url': url} for url in set(traverse_obj(
                    program, (('thumbnailLink', ('alternateProgramImage', 'document', ...)),
                              'cropInfo', ..., 'value', ..., 'url', {lambda x: x.partition('?')[0]},
                              {url_or_none}, all)))],
        }


class ABCListenAudiobookEpisodeIE(ABCListenAudiobookBaseIE):
    IE_NAME = 'abc.net.au:listen:audiobook:episode'
    _VALID_URL = r'https?://(?:www\.)?abc\.net\.au/listen/audiobooks/[^/]+/[^/]+/(?P<id>\d+)'

    _TESTS = [{
        # invalid duration
        'url': 'https://www.abc.net.au/listen/audiobooks/kids-listen-audio-stories/all-my-kisses/103170592',
        'info_dict': {
            'id': '103170592',
            'ext': 'mp3',
            'title': 'All my Kisses',
            'release_date': '20230606',
            'modified_date': '20231219',
            'description': 'md5:f853e8cf364bd8d34f4afb35fd7c11b5',
            'thumbnail': r're:^https?://live-production\.wcms\.abc-cdn\.net\.au.+',
        },
        'params': {'skip_download': True},
    }, {
        'url': 'https://www.abc.net.au/listen/audiobooks/_/chapter-2/13391438',
        'info_dict': {
            'id': '13391438',
            'ext': 'mp3',
            'title': 'How to Use This Book',
            'release_date': '20210621',
            'modified_date': '20210621',
            'description': 'md5:036f8935e04fad111bd2d8655818f66c',
            'duration': 90,
        },
        'params': {'skip_download': True},
    }]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        episode = self._call_graphql_api(video_id, 'GetEpisodeById', {'id': video_id})['episode']
        # NOTE: Unlike `GetProgramDetails`, `GetEpisodeById` also returns a `transcript` entry which
        # currently always seems to be null.
        # TODO: In case an episode with a valid `transcript` is found, extract it as TXT subtitles
        # and update `ABCListenAudiobookIE` to use `'_type': 'url'` entries so that transcripts could
        # also be extracted when downloading entire audiobooks.
        return {
            'id': video_id,
            **traverse_obj(episode, {
                'id': 'id',
                'title': (('title', 'teaserTitle', 'shortTeaserTitle', 'sortTitle'), {str}, any),
                'release_date': ('firstUpdated', {unified_strdate}),
                'modified_date': ('lastUpdated', {unified_strdate}),
                'description': ('caption', 'plainText', {str}),
                'duration': ('duration', {int_or_none}),
                'thumbnail': ('thumbnailLink', 'cropInfo', ..., 'value', ..., 'url',
                              {lambda x: x.partition('?')[0]}, {url_or_none}, any),
                'formats': ('renditions', ..., {
                    'vcodec': {lambda _: 'none'},
                    'ext': ('contentType', {mimetype2ext}),
                    'url': ('url', {url_or_none}),
                }, all),
            }),
        }

import os
from subprocess import PIPE

from yt_dlp.dependencies import mutagen
from yt_dlp.postprocessor.common import PostProcessingError, PostProcessor
from yt_dlp.utils import (
    Popen,
    hyphenate_date,
    int_or_none,
    shell_quote,
    traverse_obj,
    unified_strdate,
)


class MP4BoxPostProcessingError(PostProcessingError):
    pass


class MP4BoxPP(PostProcessor):
    def __init__(self, downloader=None, path='mp4box',
                 embed_metadata=True, embed_thumbnail=False):
        super().__init__(downloader)
        self._path = path
        self._embed_metadata = embed_metadata
        self._embed_thumbnail = embed_thumbnail
        self._delete = []

    @PostProcessor._restrict_to(images=False)
    def run(self, info):
        if not (filepath := info.get('filepath')):
            return [], info
        # mp4box replaces the input file by default
        # max length for command line is 32768 characters on Windows
        cmd = [self._path, *self._configuration_args('mp4box'),
               *self._yield_opts(info), filepath]

        self.to_screen('Remuxing fmp4 into progressive mp4 using MP4Box')
        self.write_debug(f'MP4Box command line: {shell_quote(cmd)}')
        stdout, stderr, retcode = Popen.run(
            cmd, text=True, stdout=PIPE, stderr=PIPE, stdin=PIPE)

        # mp4box writes progress to stderr
        if stdout := stdout.strip():
            self.write_debug(stdout)
        if retcode:
            self.report_warning(stderr)
            raise MP4BoxPostProcessingError(f'MP4Box exited with code {retcode}')

        if self._embed_metadata == 'mutagen':
            self._run_mutagen(info)

        return self._delete, info

    @staticmethod
    def _add_meta_prefix(key):
        return (f'meta_{key}', key)

    def _run_mutagen(self, info):
        # ref: https://mutagen.readthedocs.io/en/latest/api/mp4.html
        if not mutagen:
            self.report_warning('Mutagen is requested but is unavailable, skipping')
            return
        from mutagen.mp4 import MP4

        filepath = info['filepath']
        self.to_screen(f'Embedding extended metadata to {filepath} using mutagen')
        m4a = MP4(filepath)

        m = self._add_meta_prefix

        # freeform frames
        for key, value in traverse_obj(info, {
            'LABEL': (m('record_label'), {str}),
            'ISRC': (m('isrc'), {str}),
            'UPC': (m('upc'), {str}),
        }, get_all=False).items():
            # value must be bytes
            # https://github.com/quodlibet/mutagen/issues/391
            m4a[f'----:com.apple.iTunes:{key}'] = value.encode()

        # integer tags unrecognized by mp4box
        for key, value in traverse_obj(info, {
            'plID': (m('album_id'), {int_or_none}),
            'cnID': (m('id'), {int_or_none}),
            'atID': (('meta_artist_id', ('artist_ids', 0)), {int_or_none}),
            'geID': (('meta_genre_id', ('genre_ids', 0)), {int_or_none}),
            'sfID': (m('storefront_id'), {int_or_none}),
        }, get_all=False).items():
            m4a[key] = [value]

        m4a.save()

    def _yield_opts(self, info):
        # ref: https://cconcolato.github.io/mp4ra/filetype.html
        yield '-brand'
        if info.get('ext') == 'm4a':
            yield 'M4A :0'
        else:
            yield 'mp42'

        # add compatible brands
        for brand in ('mp42', 'isom'):
            yield '-ab'
            yield brand
        if info.get('acodec') in ('ec-3', 'eac3'):
            yield from ('-ab', 'dby1')

        # remove unwanted brands
        for brand in ('hlsf', 'ccea', 'cmfc', 'iso5'):
            yield '-rb'
            yield brand

        if lang := traverse_obj(info, (self._add_meta_prefix('language'), {str}, any)):
            yield '-lang'
            yield lang

        if tags := self._get_tags(info):
            yield '-itags'
            yield tags

    def _get_thumbnail_path(self, info):
        self._delete = []
        for t in info.get('thumbnails') or ():
            if path := t.get('filepath'):
                if os.path.exists(path):
                    if self._embed_thumbnail == 'delete':
                        self._delete.append(path)
                    return path
                self.report_warning(
                    f'Skipping embedding thumbnail {t.get("id")} because the file is missing.')
        self.to_screen('There are no thumbnails on disk.')
        return ''

    def _get_tags(self, info):
        if not self._embed_metadata and not self._embed_thumbnail:
            return ''

        if not self._embed_metadata and self._embed_thumbnail:
            if path := self._get_thumbnail_path(info):
                return f'cover={path}'
            return ''

        # get metadata
        m = self._add_meta_prefix
        yesno = {True: 'yes', False: 'no'}.get

        def age2rating(age):
            if age is None:
                return None
            if age > 17:  # explicit
                return 1
            return 2  # clean

        # ref: https://exiftool.org/TagNames/QuickTime.html
        data = traverse_obj(info, {
            'name': (m('title'), {str}),
            'album': (m('album'), {str}),
            'artist': (m('artist'), {str}),
            'album_artist': (m('album_artist'), {str}),
            'writer': (m('composer'), {str}),
            'disk': (m('disc_number'), {int_or_none}),
            'performer': (m('artist'), {str}),
            'genre': (m('genre'), {str}),
            'compilation': (m('album_type'), {str},
                            {lambda x: yesno(x.lower() == 'compilation')}),
            'created': (m('release_date'), {str}, {unified_strdate}, {hyphenate_date}),
            'rating': (m('age_limit'), {int_or_none}, {age2rating}),
            'copyright': (m('copyright'), {str}),
            # unrecognized by mediainfo
            # 'composer': 'composer',
            # 'track': 'title',
            # 'publisher': 'record_label',
            # 'isr': 'isrc',
        }, get_all=False)

        if track_number := traverse_obj(info, (m('track_number'), {int_or_none}, any)):
            if track_count := traverse_obj(info, (m('track_count'), {int_or_none}, any)):
                data['tracknum'] = f'{track_number}/{track_count}'
            else:
                data['tracknum'] = track_number

        if self._embed_thumbnail:
            if path := self._get_thumbnail_path(info):
                data['cover'] = path

        # null characters cannot be passed in command line
        # Note: mp4box provides no means to escape colons in --itags, but will
        # usually correctly infer that they are part of the previous tag.
        # In case they do cause issues, use --replace-in-metadata to change them
        # to something else.
        return ':'.join(f'{k}={v}'.replace('\0', '') for k, v in data.items())

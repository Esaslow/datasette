from contextlib import contextmanager
import base64
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib
import shlex


def compound_pks_from_path(path):
    return [
        urllib.parse.unquote_plus(b) for b in path.split(',')
    ]


def path_from_row_pks(row, pks, use_rowid):
    if use_rowid:
        return urllib.parse.quote_plus(str(row['rowid']))
    bits = []
    for pk in pks:
        bits.append(
            urllib.parse.quote_plus(str(row[pk]))
        )
    return ','.join(bits)


def build_where_clauses(args):
    sql_bits = []
    params = {}
    for i, (key, value) in enumerate(sorted(args.items())):
        if '__' in key:
            column, lookup = key.rsplit('__', 1)
        else:
            column = key
            lookup = 'exact'
        template = {
            'exact': '"{}" = :{}',
            'contains': '"{}" like :{}',
            'endswith': '"{}" like :{}',
            'startswith': '"{}" like :{}',
            'gt': '"{}" > :{}',
            'gte': '"{}" >= :{}',
            'lt': '"{}" < :{}',
            'lte': '"{}" <= :{}',
            'glob': '"{}" glob :{}',
            'like': '"{}" like :{}',
        }[lookup]
        numeric_operators = {'gt', 'gte', 'lt', 'lte'}
        value_convert = {
            'contains': lambda s: '%{}%'.format(s),
            'endswith': lambda s: '%{}'.format(s),
            'startswith': lambda s: '{}%'.format(s),
        }.get(lookup, lambda s: s)
        converted = value_convert(value)
        if lookup in numeric_operators and converted.isdigit():
            converted = int(converted)
        param_id = 'p{}'.format(i)
        sql_bits.append(
            template.format(column, param_id)
        )
        params[param_id] = converted
    return sql_bits, params


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, sqlite3.Row):
            return tuple(obj)
        if isinstance(obj, sqlite3.Cursor):
            return list(obj)
        if isinstance(obj, bytes):
            # Does it encode to utf8?
            try:
                return obj.decode('utf8')
            except UnicodeDecodeError:
                return {
                    '$base64': True,
                    'encoded': base64.b64encode(obj).decode('latin1'),
                }
        return json.JSONEncoder.default(self, obj)


@contextmanager
def sqlite_timelimit(conn, ms):
    deadline = time.time() + (ms / 1000)
    # n is the number of SQLite virtual machine instructions that will be
    # executed between each check. It's hard to know what to pick here.
    # After some experimentation, I've decided to go with 1000 by default and
    # 1 for time limits that are less than 50ms
    n = 1000
    if ms < 50:
        n = 1

    def handler():
        if time.time() >= deadline:
            return 1

    conn.set_progress_handler(handler, n)
    yield
    conn.set_progress_handler(None, n)


class InvalidSql(Exception):
    pass


def validate_sql_select(sql):
    sql = sql.strip().lower()
    if not sql.startswith('select '):
        raise InvalidSql('Statement must begin with SELECT')
    if 'pragma' in sql:
        raise InvalidSql('Statement may not contain PRAGMA')


def path_with_added_args(request, args):
    current = request.raw_args.copy()
    current.update(args)
    return request.path + '?' + urllib.parse.urlencode(current)


def path_with_ext(request, ext):
    path = request.path
    path += ext
    if request.query_string:
        path += '?' + request.query_string
    return path


_css_re = re.compile(r'''['"\n\\]''')
_boring_table_name_re = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def escape_css_string(s):
    return _css_re.sub(lambda m: '\\{:X}'.format(ord(m.group())), s)


def escape_sqlite_table_name(s):
    if _boring_table_name_re.match(s):
        return s
    else:
        return '[{}]'.format(s)


def make_dockerfile(files, metadata_file, extra_options=''):
    cmd = ['"datasette"', '"serve"', '"--host"', '"0.0.0.0"']
    cmd.append('"' + '", "'.join(files) + '"')
    cmd.extend(['"--cors"', '"--port"', '"8001"', '"--inspect-file"', '"inspect-data.json"'])
    if metadata_file:
        cmd.extend(['"--metadata"', '"{}"'.format(metadata_file)])
    if extra_options:
        for opt in extra_options.split():
            cmd.append('"{}"'.format(opt))
    return '''
FROM python:3
COPY . /app
WORKDIR /app
RUN pip install datasette
RUN datasette build {} --inspect-file inspect-data.json
EXPOSE 8001
CMD [{}]'''.format(
        ' '.join(files),
        ', '.join(cmd)
    ).strip()


@contextmanager
def temporary_docker_directory(files, name, metadata, extra_options, extra_metadata=None):
    extra_metadata = extra_metadata or {}
    tmp = tempfile.TemporaryDirectory()
    # We create a datasette folder in there to get a nicer now deploy name
    datasette_dir = os.path.join(tmp.name, name)
    os.mkdir(datasette_dir)
    saved_cwd = os.getcwd()
    file_paths = [
        os.path.join(saved_cwd, name)
        for name in files
    ]
    file_names = [os.path.split(f)[-1] for f in files]
    if metadata:
        metadata_content = json.load(metadata)
    else:
        metadata_content = {}
    for key, value in extra_metadata.items():
        if value:
            metadata_content[key] = value
    try:
        dockerfile = make_dockerfile(file_names, metadata_content and 'metadata.json', extra_options)
        os.chdir(datasette_dir)
        if metadata_content:
            open('metadata.json', 'w').write(json.dumps(metadata_content, indent=2))
        open('Dockerfile', 'w').write(dockerfile)
        for path, filename in zip(file_paths, file_names):
            os.link(path, os.path.join(datasette_dir, filename))
        yield
    finally:
        tmp.cleanup()
        os.chdir(saved_cwd)

@contextmanager
def temporary_heroku_directory(files, name, metadata, extra_options, extra_metadata=None):
    # FIXME: lots of duplicated code from above

    extra_metadata = extra_metadata or {}
    tmp = tempfile.TemporaryDirectory()
    saved_cwd = os.getcwd()

    file_paths = [
        os.path.join(saved_cwd, name)
        for name in files
    ]
    file_names = [os.path.split(f)[-1] for f in files]

    if metadata:
        metadata_content = json.load(metadata)
    else:
        metadata_content = {}
    for key, value in extra_metadata.items():
        if value:
            metadata_content[key] = value

    try:
        os.chdir(tmp.name)

        if metadata_content:
            open('metadata.json', 'w').write(json.dumps(metadata_content, indent=2))

        open('runtime.txt', 'w').write('python-3.6.2')
        open('requirements.txt', 'w').write('datasette')
        os.mkdir('bin')
        open('bin/post_compile', 'w').write('datasette build --inspect-file inspect-data.json')

        quoted_files = " ".join(map(shlex.quote, files))
        procfile_cmd = f'web: datasette serve --host 0.0.0.0 {quoted_files} --cors --port $PORT --inspect-file inspect-data.json'
        open('Procfile', 'w').write(procfile_cmd)

        for path, filename in zip(file_paths, file_names):
            os.link(path, os.path.join(tmp.name, filename))

        yield

    finally:
        tmp.cleanup()
        os.chdir(saved_cwd)


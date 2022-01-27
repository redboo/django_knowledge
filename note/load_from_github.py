import io
import zipfile
import os.path

import firebase_admin
import requests
from firebase_admin import credentials, firestore
from typesense import Client

from note.models import Note


def download_from_github_archive(owner, repo, directory):
    response = requests.get('https://github.com/{}/{}/archive/refs/heads/main.zip'.format(owner, repo))
    archive = io.BytesIO(response.content)
    with zipfile.ZipFile(archive) as archive_object:
        for member_name in archive_object.namelist():
            if not member_name.startswith('{}-main/{}/'.format(repo, directory)):
                continue

            member_info = archive_object.getinfo(member_name)
            if not member_info.is_dir():
                with archive_object.open(member_info) as member_file:
                    file_name, _ = os.path.splitext(os.path.basename(member_name))
                    file_content = str(member_file.read(), 'utf-8')
                    yield file_name, file_content


def download_from_github_directory(owner, repo, directory, token):
    graphql = """query getStartAndEndPoints {{
  repository(owner: "{}", name: "{}") {{
    object(expression: "HEAD:{}") {{
      ... on Tree {{
        entries {{
          name
          object {{
            ... on Blob {{
              text
            }}
          }}
        }}
      }}
    }}
  }}
}}""".format(owner, repo, directory)
    response = requests.post('https://api.github.com/graphql', json={'query': graphql}, headers={
        'Content-Type': 'application/json',
        'Authorization': 'bearer {}'.format(token),
        'User-Agent': 'test',
    })
    content = response.json()
    if 'message' in content:
        print(content['message'])

    errors = content.get('errors')
    if errors:
        for error in errors:
            print(error['message'])

    data = content.get('data')
    if data:
        files = data['repository']['object']['entries']
        for file in files:
            file_name = file['name']
            file_name, _ = os.path.splitext(file_name)
            file_content = file['object']['text']
            yield file_name, file_content


class UploaderFirestore:
    MAX_PORTION_SIZE = 500

    def __init__(self, certificate):
        cred = credentials.Certificate(certificate)
        firebase_admin.initialize_app(cred)
        self.db = firestore.client()
        self.batch = self.db.batch()

    def clear(self):
        ...

    def add_to_portion(self, file_name, file_content):
        ref = self.db.collection('knowledge').document(file_name)
        self.batch.set(ref, {'text': file_content})

    def commit(self):
        self.batch.commit()


class UploaderTypesense:
    MAX_PORTION_SIZE = 500
    portion = []
    knowledge_schema = {
        'name': 'knowledge',
        'fields': [
            {'name': 'filename', 'type': 'string'},
            {'name': 'text', 'type': 'string', 'facet': True},
            {'name': 'index', 'type': 'float'}
        ],
        'default_sorting_field': 'index'
    }
    index = 0

    def __init__(self, server, port, protocol, api_key):
        self.client = Client({
            'nodes': [{
                'host': server,
                'port': port,
                'protocol': protocol,
            }],
            'api_key': api_key,
            'connection_timeout_seconds': 2
        })

    def clear(self):
        try:
            name = self.knowledge_schema['name']
            self.client.collections[name].delete()
        except Exception as e:
            pass

        self.client.collections.create(self.knowledge_schema)

    def add_to_portion(self, file_name, file_content):
        fields = {'filename': file_name, 'text': file_content, 'index': self.index}
        self.portion.append(fields)
        self.index += 1

    def commit(self):
        name = self.knowledge_schema['name']
        self.client.collections[name].documents.import_(self.portion)
        self.portion.clear()

    def search(self, file_name=None, file_content=None, page_number=1):
        name = self.knowledge_schema['name']
        res = self.client.collections[name].documents.search({
            'q': file_name,
            'query_by': 'filename',
            'sort_by': 'index:desc'
        })
        results = []
        for hit in res['hits']:
            document = hit['document']
            results.append(dict(title=document['filename'], content=document['text']))

        return dict(results=results, count=res['found'])


class UploaderDjangoServer:
    MAX_PORTION_SIZE = 400
    portion = []

    def __init__(self):
        pass

    def clear(self):
        Note.objects.all().delete()

    def add_to_portion(self, file_name, file_content):
        fields = Note(title=file_name, content=file_content)
        self.portion.append(fields)

    def commit(self):
        Note.objects.bulk_create(self.portion, self.MAX_PORTION_SIZE)
        self.portion.clear()

    def search(self, file_name=None, file_content=None, page_number=1):
        filter = {}
        if file_name:
            filter['title__contains'] = file_name

        if file_content:
            filter['content__contains'] = file_content

        results = list(Note.objects.filter(**filter).values('title', 'content'))
        return dict(results=results, count=len(results))


def get_class_name(camel_case):
    return 'Uploader{}'.format(camel_case.title().replace('_', ''))


def run_initiator(downloader, args_downloader, uploader, args_uploader):
    downloader = globals()['download_from_{}'.format(downloader)]
    uploader = globals()[get_class_name(uploader)](*args_uploader)
    uploader.clear()
    portion_size = 0
    for file_name, file_content in downloader(*args_downloader):
        uploader.add_to_portion(file_name, file_content)
        portion_size += 1
        if portion_size == uploader.MAX_PORTION_SIZE:
            uploader.commit()
            portion_size = 0

    if portion_size:
        uploader.commit()


def search(uploader, args_uploader, file_name=None, file_content=None):
    uploader = globals()[get_class_name(uploader)](*args_uploader)
    return uploader.search(file_name, file_content)
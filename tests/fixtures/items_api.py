from contextlib import closing
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sqlite3


class ItemsHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, database):
        self.database = database
        super().__init__(*args)

    def do_POST(self):
        if self.path != '/items':
            self.send_error(404)
            return
        name = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['name']
        if not name:
            self.reply(422, b'name required')
            return
        with closing(sqlite3.connect(self.database)) as store, store:
            store.execute('INSERT INTO items VALUES (?)', (name,))
        self.reply(201, b'created')

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def create_server(database):
    with closing(sqlite3.connect(database)) as store, store:
        store.execute('CREATE TABLE items (name TEXT NOT NULL)')
    return HTTPServer(('localhost', 0), partial(ItemsHandler, database=database))

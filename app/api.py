"""JSON API и Swagger; сайт продолжает использовать свои простые формы."""
from flask import Blueprint, abort, jsonify, request
from flask_swagger_ui import get_swaggerui_blueprint
from werkzeug.exceptions import HTTPException


def register_api(app):
    from app import create_matter, db, download, health, upload
    api = Blueprint('api', __name__, url_prefix='/api')

    @api.get('/matters')
    def matters():
        with db() as conn:
            return jsonify(conn.execute('SELECT * FROM matters ORDER BY id').fetchall())

    @api.get('/files')
    def files():
        matter, search = request.args.get('matter', type=int), request.args.get('q', '')[:150]
        with db() as conn:
            rows = conn.execute('''SELECT f.*,m.number,m.client FROM files f JOIN matters m ON m.id=f.matter_id
                WHERE (%s::INTEGER IS NULL OR f.matter_id=%s) AND (f.title ILIKE %s OR f.filename ILIKE %s)
                ORDER BY f.id DESC''', (matter, matter, f'%{search}%', f'%{search}%')).fetchall()
            return jsonify(rows)

    @api.get('/files/<int:file_id>')
    def file_info(file_id):
        with db() as conn:
            row = conn.execute('SELECT * FROM files WHERE id=%s', (file_id,)).fetchone()
        return jsonify(row) if row else abort(404, description='Документ не найден.')

    api.add_url_rule('/health', 'health', health)
    api.add_url_rule('/matters', 'create_matter', create_matter, methods=['POST'])
    api.add_url_rule('/files', 'upload', upload, methods=['POST'])
    api.add_url_rule('/files/<int:file_id>/download', 'download', download)

    def parameter(name, kind='string', where='query'):
        return {'name': name, 'in': where, 'required': where == 'path', 'schema': {'type': kind}}

    def operation(summary, code='200', parameters=None, body=None):
        value = {'summary': summary, 'responses': {code: {'description': 'Успешно'},
                 '401': {'description': 'Неверный логин или пароль'}, '400': {'description': 'Ошибка запроса'}}}
        if parameters:
            value['parameters'] = parameters
        if body:
            value['requestBody'] = {'required': True, 'content': body}
        return value

    def form(properties, media):
        return {media: {'schema': {'type': 'object', 'required': list(properties), 'properties': properties}}}

    text = {'type': 'string'}
    spec = {
        'openapi': '3.0.3',
        'info': {'title': 'API архива документов', 'version': '',
                 'description': 'Нажмите Authorize и введите admin и пароль из .env (тот же, что для сайта). Файлы до 25 МБ.'},
        'servers': [{'url': '/'}],
        'components': {'securitySchemes': {'basicAuth': {'type': 'http', 'scheme': 'basic'}}},
        'security': [{'basicAuth': []}],
        'paths': {
            '/api/health': {'get': {**operation('Состояние БД, Redis и хранилища'), 'security': []}},
            '/api/matters': {
                'get': operation('Список дел'),
                'post': operation('Создать дело', '201', body=form({'number': text, 'title': text, 'client': text}, 'application/x-www-form-urlencoded'))},
            '/api/files': {
                'get': operation('Список документов', parameters=[parameter('matter', 'integer'), parameter('q')]),
                'post': operation('Загрузить документ', '201', body=form({'title': text, 'matter': {'type': 'integer'},
                                   'file': {'type': 'string', 'format': 'binary'}}, 'multipart/form-data'))},
            '/api/files/{file_id}': {'get': operation('Сведения и статус проверки', parameters=[parameter('file_id', 'integer', 'path')])},
            '/api/files/{file_id}/download': {'get': operation('Скачать документ', parameters=[parameter('file_id', 'integer', 'path')])},
        },
    }
    spec['paths']['/api/files/{file_id}/download']['get']['responses']['200'] = {
        'description': 'Файл', 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}

    @api.get('/openapi.json')
    def openapi():
        return jsonify(spec)

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith('/api/') and not request.path.startswith('/api/docs'):
            return jsonify(error=error.description), error.code
        return error

    app.register_blueprint(api)
    app.register_blueprint(get_swaggerui_blueprint('/api/docs', '/api/openapi.json',
        config={'app_name': 'API архива документов', 'validatorUrl': None, 'persistAuthorization': False}))

from __future__ import print_function

import os
import json
import glob
import time
import logging
import inspect
import requests
import base64
import hashlib
import datetime
import tempfile
import nbformat

from io import BytesIO


from flask import Flask, make_response, jsonify, request, url_for, send_file, Response
from flask.json import JSONEncoder
from flask_caching import Cache
from flask_cors import CORS

from flasgger import LazyJSONEncoder, LazyString, Swagger, swag_from

from logging.config import dictConfig

from nb2workflow.workflows import serialize_workflow_exception

import threading  

dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    }},
    'handlers': {'wsgi': {
        'class': 'logging.StreamHandler',
  #      'stream': 'ext://flask.logging.wsgi_errors_stream',
        'formatter': 'default'
    }},
    'root': {
        'level': 'INFO',
        'handlers': ['wsgi']
    }
})

verify_tls = False

from nb2workflow.nbadapter import NotebookAdapter, find_notebooks
from nb2workflow import ontology, publish, schedule
    
logger=logging.getLogger('nb2workflow.service')

class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        script_name = environ.get('HTTP_X_FORWARDED_PREFIX', '')
        if script_name:
            environ['SCRIPT_NAME'] = script_name
            path_info = environ['PATH_INFO']
            if path_info.startswith(script_name):
                environ['PATH_INFO'] = path_info[len(script_name):]

        scheme = environ.get('HTTP_X_SCHEME', '')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        return self.app(environ, start_response)


class CustomJSONEncoder(LazyJSONEncoder):
    def default(self, obj, *args, **kwargs):
        try:
            if isinstance(obj, type):
                return dict(type_object=repr(obj))
            iterable = iter(obj)
        except TypeError:
            pass
        else:
            return list(iterable)
        return JSONEncoder.default(self, obj)

cache = Cache(config={'CACHE_TYPE': 'simple'})

def create_app():
    app=Flask(__name__)
    template = dict(swaggerUiPrefix=LazyString(lambda : request.environ.get('HTTP_X_FORWARDED_PREFIX', '')))
    swagger = Swagger(app, template=template)
    app.wsgi_app = ReverseProxied(app.wsgi_app)
    app.json_encoder = CustomJSONEncoder
    cache.init_app(app, config={'CACHE_TYPE': 'simple'})
#    CORS(app)
    return app


app = create_app()

app.async_workflows = dict()
app.started_at = datetime.datetime.now()

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

def make_key():
    """Make a key that includes GET parameters."""
    return request.full_path


class AsyncWorkflow(threading.Thread):
    def __init__(self, key, target, params):
        self.key = key
        self.target = target
        self.params = params
        super(AsyncWorkflow, self).__init__()

    def run(self):
        try:
            self._run()
        except Exception as e:
            print("failed",e)

    def _run(self):
        template_nba = app.notebook_adapters.get(self.target)

        nba = NotebookAdapter(template_nba.notebook_fn)

        exceptions = nba.execute(self.params['request_parameters'])

        logger.info("exceptions: %s",repr(exceptions))

        if len(exceptions)>0:
            output='incomplete'
            logger.error("exceptions: %s",repr(exceptions))
        else:
            nretry=10
            while nretry>0:
                try:
                    output=nba.extract_output()
                    logger.info("completed, output length %s",len(output))
                    if len(output) == 0:
                        logger.debug("output from notebook is empty, something failed, attempts left:", nretry)
                    else:
                        break
                except Exception as e:
                    logger.debug("output notebook incomplte or does not exist", e, "attempts left:", nretry)
                except nbformat.reader.NotJSONError as e:
                    logger.debug("output notebook incomplte", e, "attempts left:", nretry)

                nretry-=1
                time.sleep(1)

        logger.error("output: %s",output)
        
        logger.info("updating key %s",self.key)
        app.async_workflows[self.key] = dict(output=output, exceptions=list(map(serialize_workflow_exception, exceptions)), jobdir=nba.tmpdir)


def workflow(target, background=False, async_request=False):
    issues = []

    async_request = request.args.get('_async_request', async_request)
    
    logger.debug("target %s",target)

    if not background:
        logger.debug("raw parameters %s",request.args)

    template_nba = app.notebook_adapters.get(target)
    nba = NotebookAdapter(template_nba.notebook_fn)

    if nba is None:
        issues.append("target not known: %s; available targets: %s"%(target,app.notebook_adapters.keys()))
    else:
        if not background:
            interpreted_parameters = nba.interpret_parameters(request.args)
            issues += interpreted_parameters['issues']
        else:
            interpreted_parameters = dict(request_parameters=[])

    logger.debug("interpreted parameters %s",interpreted_parameters)


    ## async
    if async_request:
        key = hashlib.sha224(json.dumps(dict(target=target, params=interpreted_parameters)).encode('utf-8')).hexdigest()
        
        value = app.async_workflows.get(key, None)

        print('cache key/value',key,value)
    
        if value is None:
            async_task = AsyncWorkflow(key=key, target=target, params=interpreted_parameters)
            async_task.start()
            app.async_workflows[key]='started'
            return make_response(jsonify(workflow_status="submitted", comment="task created"), 201)

        elif value == 'started':
            return make_response(jsonify(workflow_status="started", comment="task created before"), 201)

        else:
            return make_response(jsonify(workflow_status="done", data=value, comment=""), 200)


    if len(issues)>0:
        return make_response(jsonify(issues=issues), 400)
    else:
        exceptions = nba.execute(interpreted_parameters['request_parameters'])

        nretry=10
        while nretry>0:
            try:
                output=nba.extract_output()
                if len(output) == 0:
                    logger.debug("output from notebook is empty, something failed, attempts left:", nretry)
                else:
                    break
            except nbformat.reader.NotJSONError as e:
                logger.debug("output notebook incomplte", e, "attempts left:", nretry)

            nretry-=1
            time.sleep(1)
        

        logger.debug("output: %s",output)
        logger.debug("exceptions: %s",exceptions)

        r = jsonify(dict(
                    output=output,
                    exceptions=[repr(e) for e in exceptions],
                    jobdir=nba.tmpdir,
                ))

        return_code = 200
        if len(exceptions) > 0:
            return_code = 500

        return r, return_code


def to_oapi_type(in_type):
    out_type='string'

    if issubclass(in_type,int):
        out_type='integer'

    if issubclass(in_type,float):
        out_type='number'
    
    if issubclass(in_type,str):
        out_type='string'
    
    logger.debug("oapi type cast from %s to %s",repr(in_type),repr(out_type))
    
    return out_type

from werkzeug.routing import RequestRedirect, MethodNotAllowed, NotFound

def get_view_function(url, method='GET'):
    """Match a url and return the view and arguments
    it will be called with, or None if there is no view.
    """

    adapter = app.url_map.bind('localhost')

    try:
        match = adapter.match(url, method=method)
    except RequestRedirect as e:
        # recursively match redirects
        return get_view_function(e.new_url, method)
    except (MethodNotAllowed, NotFound):
        # no match
        return None

    try:
        # return the view function and arguments
        return app.view_functions[match[0]], match[1]
    except KeyError:
        # no view is associated with the endpoint
        return None

def setup_routes(app):
    for target, nba in app.notebook_adapters.items():
        target_specs=specs_dict = {
              "parameters": [
                {
                  "name": p_name,
                  "in": "query",
                  "type": to_oapi_type(p_data['python_type']),
                  "required": "false",
                  "default": p_data['default_value'],
                  "description": p_data['comment']+" "+p_data['owl_type'],
                }
                for p_name, p_data in nba.extract_parameters().items()
              ],
              "responses": {
                "200": {
                  "description": repr(nba.extract_output_declarations()),
                }
              }
            }

        endpoint='endpoint_'+target


        def funcg(target):
            def workflow_func():
                return workflow(target)
            return workflow_func

        logger.debug("target: %s with endpoint %s",target,endpoint)

        def response_filter(rv):
            if isinstance(rv, tuple) and isinstance(rv[0], Response) and rv[1] != 200:
                logger.info("NOT caching response %s", rv[1])
                return False
            elif isinstance(rv, Response) and rv.status != 200:
                logger.info("NOT caching response %s", rv)
                return False
            else:
                logger.info("caching response %s", rv)
                return True

        cache_timeout = nba.get_system_parameter_value('cache_timeout', 0)
        try:
            app.route('/api/v1.0/get/'+target,methods=['GET'],endpoint=endpoint)(
            swag_from(target_specs)(
            cache.cached(timeout=cache_timeout,key_prefix=make_key,response_filter=response_filter,query_string=True)(
                funcg(target)
            )))
        except AssertionError as e:
            logger.info("unable to add route:",e)
            raise

        schedule_interval = nba.get_system_parameter_value('schedule_interval', 0)
        if schedule_interval>0:
            logger.info("scheduling callable %s every %lg", str(funcg), float(schedule_interval))

            def schedulable():
                with app.test_request_context('/api/v1.0/get/'+target):
                    from flask import request
                    get_view_function('/api/v1.0/get/'+target)[0]()

            schedule.schedule_callable(schedulable, schedule_interval)

# list input -> output function signatures and identities

@app.route('/api/v1.0/options',methods=['GET'])
def workflow_options():
    return jsonify(dict([
                    (
                        target,
                        dict(output=nba.extract_output_declarations(),parameters=nba.extract_parameters()),
                    )
                     for target, nba in app.notebook_adapters.items()
                    ]))

@app.route('/api/v1.0/get-<mode>/<target>/<filename>',methods=['GET'])
def workflow_filename(mode, target, filename):

    target_url = url_for('endpoint_'+target,_external=True,**request.args)

    # report equivalency

    if "HTTP_AUTH" in os.environ:
        username, password = os.environ.get("HTTP_AUTH").split(":")
        auth=requests.auth.HTTPBasicAuth(username, password)
    else:
        auth=None

    r = requests.get(target_url, verify = verify_tls, auth = auth)
    
    try:
        rj = r.json()
        logger.info(rj.keys())

        if 'data' in rj:
            output = rj.get('data').get('output')
        elif 'output' in rj:
            output = rj.get('output')
        else:
            return jsonify(rj)

        if filename+'_content' in output:
            content = base64.b64decode(base64.b64decode(output.get(filename+'_content',None)))
        else:
            return jsonify({'workflow_status':'anomaly', 'comment': 'searching for key '+filename+'_content'+', available: '+(", ".join(output.keys())), 'base_workflow_result':rj })

        if content:
            if mode == "file" or mode == "png":
                return send_file(BytesIO(content), mimetype='image/png')
            elif mode == "html":
                return content
            else:
                return 404
        else:
            return jsonify(dict(
                        exceptions=["no such file, available:",output.keys()],
                    ))

    except Exception as e:
        logger.error("problem decoding: %s",repr(e))
        return jsonify(dict(
                    exceptions={"problem decoding":repr(e),"raw_response":r.content.decode('utf-8')},
                ))

@app.route('/api/v1.0/rdf',methods=['GET'])
def workflow_rdf():
    return make_response(app.service_semantic_signature)

@app.route('/health')
def healthcheck():
    issues=[]
    status = {}

    statvfs = os.statvfs(".")
    status['fs_space'] = dict(
        size_mb = statvfs.f_frsize * statvfs.f_blocks / 1024 / 1024,
        avail_mb = statvfs.f_frsize * statvfs.f_bavail / 1024 / 1024,
    )

    if status['fs_space']['avail_mb'] < 300:
        issues.append("not enough free space: %.5lg Mb left"%status['fs_space']['avail_mb'])


    import psutil

    processes = []
    status['n_open_files'] = 0
    status['n_processes'] = 0
    status['n_threads'] = 0

    for proc in psutil.process_iter():
        try:
            processes.append(dict(
                    n_open_files = len(proc.open_files()),
                ))

            status['n_open_files'] += len(proc.open_files())
            status['n_processes'] += 1
            status['n_threads'] += proc.num_threads()
        except Exception as e:
            pass

    status['cpu_times'] = dict(psutil.cpu_times_percent()._asdict())

    status['loadavg'] = psutil.getloadavg()

    if max(status['loadavg']) > 10:
        issues.append("high load avg: %s"%repr(status['loadavg']))


    status['disk_usage'] = dict([ (k+"_mb", v/1024/1024) if k!="percent" else (k,v) for k,v in dict(psutil.disk_usage(".")._asdict()).items()])


    #status['processes'] = processes

    if len(issues)==0:
        return jsonify(dict(summary="all is ok!",status=status))
    else:
        return make_response(jsonify(issues=issues), 500)

@app.route('/test')
def test():
    results = {}
    expecting = []

    for template_nba in app.notebook_adapters.values():
        if template_nba.name.startswith('test_'):
            key = template_nba.name

            if key in app.async_workflows:
                print("found", app.async_workflows[key])

                if isinstance(app.async_workflows[key], dict):
                    print("found result seems a reasonable dict")
                    workflow_status = app.async_workflows[key].get('workflow_status', 'done')
                    print("workflow_status", workflow_status)
                else:
                    workflow_status = app.async_workflows[key]

                if workflow_status == 'done':
                    results[template_nba.name] = app.async_workflows[key]['exceptions'] # and output notebook
                    print("workflow_status is done, results exceptions:", results[template_nba.name])
                else:
                    expecting.append(dict(key = key, workflow_status=workflow_status))
            else:
                async_task = AsyncWorkflow(key=key, target=template_nba.name, params=dict(request_parameters=dict(location=os.path.dirname(template_nba.notebook_fn))))
                async_task.start()
                app.async_workflows[key]='started'
                expecting.append(dict(key = key, workflow_status='submitted'))


    if expecting != [] :
        return make_response(jsonify(dict(expecting=expecting)), 201)
    else:
        if all([v in ['[]', []] for v in results.values()]):
            print("tests passed")
            return make_response('all is OK: '+"; ".join(results.keys()), 200)
        else:
            print("tests NOT passed")
            return make_response(jsonify(results), 500)


@app.route('/')
def root():
    issues=[]

    if len(issues)==0:
        return "all is ok!"
    else:
        return make_response(jsonify(issues=issues), 500)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('notebook', metavar='notebook', type=str)
    parser.add_argument('--host', metavar='host', type=str, default="127.0.0.1")
    parser.add_argument('--port', metavar='port', type=int, default=9191)
    #parser.add_argument('--tmpdir', metavar='tmpdir', type=str, default=None)
    parser.add_argument('--publish', metavar='upstream-url', type=str, default=None)
    parser.add_argument('--publish-as', metavar='published url', type=str, default=None)
    parser.add_argument('--profile', metavar='service profile', type=str, default="oda")
    parser.add_argument('--debug', action="store_true")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger("nb2workflow").setLevel(level=logging.DEBUG)
        logging.getLogger("flask").setLevel(level=logging.DEBUG)

    app.notebook_adapters = find_notebooks(args.notebook)
    setup_routes(app)
    app.service_semantic_signature=ontology.service_semantic_signature(app.notebook_adapters)

    if args.publish:
        logger.info("publishing to %s",args.publish)

        if args.publish_as:
            s = args.publish_as.split(":")
            publish_host, publish_port = ":".join(s[:-1]), int(s[-1])
        else:
            publish_host, publish_port = args.host, args.port

        for nba_name, nba in app.notebook_adapters.items():
            publish.publish(args.publish, nba_name, publish_host, publish_port)


  #  for rule in app.url_map.iter_rules():
 #       logger.debug("==>> %s %s %s %s",rule,rule.endpoint,rule.__class__,rule.__dict__)

    app.run(host=args.host,port=args.port)

@app.route('/status')
def status():
    return jsonify(
                version = os.environ.get('WORKFLOW_VERSION','unknown'),
                started_at = app.started_at.strftime("%s"),
                started_since = (datetime.datetime.now()-app.started_at).seconds,
                background_jobs = len([w for w in app.async_workflows if w]),
                stored_jobs = len(app.async_workflows),
            )

@app.route('/async/delete')
def async_delete():
    return jsonify(app.async_workflows)

@app.route('/async/clear')
def async_clear():
    s = app.async_workflows
    app.async_workflows = dict()
    return jsonify(s)

@app.route('/async/list')
def async_list():
    return jsonify(app.async_workflows)

def get_trace_list():
    r=[]
    for d in glob.glob(os.path.join(tempfile.gettempdir(),"tmp*")):
        r.append(dict(fn=d, mtime=os.stat(d).st_mtime, ctime=os.stat(d).st_ctime))
    return sorted(r, key=lambda x:['ctime'])

@app.route('/trace/list')
def trace_list():
    return jsonify(get_trace_list())

@app.route('/trace/<string:job>')
def trace_get(job):
    r = []

    r = []
    for fn in glob.glob(os.path.join(tempfile.gettempdir(), job,"*_output.ipynb")):
        r.append(fn)

    return jsonify(r)


@app.route('/trace/<string:job>/<string:func>')
def trace_get_func(job, func):
    if func == "custom.css":
        return ""

    from nbconvert.exporters import HTMLExporter
    exporter = HTMLExporter()
   
    fn = os.path.join(tempfile.gettempdir(), job, func+"_output.ipynb")

    output, resources = exporter.from_filename(fn)

    return output

@app.route('/clear-cache')
def clear_cache():
    n_entries = None
    try:
        n_entries = len(cache.cache._cache)
    except Exception as e:
        pass
    
    cache.clear()

    if n_entries is not None:
        return 'cleared %i entries'%n_entries
    else:
        return 'cleared some entries'

if __name__ == '__main__':
    main()


# Copyright (C) 2022 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

import json
import logging
import os
import subprocess
import urllib.parse

import kubernetes.client
import kubernetes.config
import requests
from flask import Flask, abort, request
from flask.helpers import send_file, send_from_directory
from flask.json import jsonify
from flask_httpauth import HTTPTokenAuth
from werkzeug.datastructures import Headers

application = app = Flask(__name__)

if __name__ != "__main__":
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)

app.config["NO_TOKENS"] = os.getenv("NO_TOKENS") == "1"
app.config["DAEMONSET"] = os.getenv("DAEMONSET") == "1"
if port := os.getenv("KOREDUMP_DAEMONSET_PORT"):
    app.config["DAEMONSET_PORT"] = int(port)
auth = HTTPTokenAuth()

cores = {}
cores_stat = None

decompression_methods = {
    ".lz4": ["lz4", "-c", "-q", "-d"],
    ".xz": ["xz", "-c", "-q", "-d"],
    ".zst": ["zstd", "-c", "-q", "-d"],
}


@auth.verify_token
def verify_token(token):
    if app.config["NO_TOKENS"]:
        return True

    if len(token) == 0:
        return False

    try:
        body = kubernetes.client.V1TokenReview(spec={"token": token})
        ret: kubernetes.client.V1TokenReview = (
            kubernetes.client.AuthenticationV1Api().create_token_review(body)
        )
        return ret.status.authenticated

    except Exception as ex:
        app.logger.debug("Token review failed: %s", ex)
        return False


def read_cores():
    """
    Read available cores from /koredump/index.json
    The file is generated by koremonitor.
    """

    if not app.config["DAEMONSET"]:
        return

    global cores, cores_stat
    index_path = "/koredump/index.json"

    try:
        st = os.stat(index_path)
        if cores_stat:
            if st.st_mtime <= cores_stat.st_mtime and st.st_size == cores_stat.st_size:
                return
            app.logger.info("File has changed, reloading: %s", index_path)
        cores_stat = st

        with open(index_path) as fp:
            cores = json.load(fp)

        app.logger.info("Reloaded %s with %d cores.", index_path, len(cores))

    except Exception:
        pass


if app.config["DAEMONSET"]:
    read_cores()

if not os.getenv("FAKE_K8S"):
    kubernetes.config.load_incluster_config()


def get_ds_pods():
    return kubernetes.client.CoreV1Api().list_pod_for_all_namespaces(
        label_selector="koredump.daemonset=1"
    )


def get_ds_pod_ips():
    if os.getenv("FAKE_K8S"):
        return ["127.0.0.1"]

    pods = get_ds_pods()
    ret = []
    for pod in pods.items:
        ret.append(pod.status.pod_ip)
    return ret


def get_ds_pod_ip(node_name):
    if os.getenv("FAKE_K8S"):
        return "127.0.0.1"

    pods = get_ds_pods()
    for pod in pods.items:
        if pod.spec.node_name == node_name:
            return pod.status.pod_ip
    return None


@app.get("/health")
def health():
    return "OK\n"


def filtered_core_metadata(core):
    """Filter out some internal metadata, to avoid returning them via REST API."""
    core = core.copy()
    for key in ["_systemd_coredump", "_systemd_journal", "_core_dir"]:
        if key in core:
            del core[key]
    return core


def sorted_cores(cores):
    """List of cores sorted by timestamp, oldest core is last."""
    return sorted(
        cores,
        key=lambda core: core["COREDUMP_TIMESTAMP"]
        if "COREDUMP_TIMESTAMP" in core
        else core["id"],
    )


if app.config["DAEMONSET"]:

    @app.get("/apiv1/cores")
    @auth.login_required
    def get_cores():
        arg_namespace = request.args.get("namespace", "")
        arg_pod = request.args.get("pod", "")
        read_cores()
        ret = []
        for core_id in cores:
            if "_DELETED" in cores[core_id]:
                continue
            if len(arg_namespace) > 0 and arg_namespace != cores[core_id].get(
                "namespace"
            ):
                continue
            if len(arg_pod) > 0 and arg_pod != cores[core_id].get("pod"):
                continue
            ret.append(filtered_core_metadata(cores[core_id]))
        return jsonify(sorted_cores(ret))

    @app.get("/apiv1/cores/metadata/<string:core_id>")
    @auth.login_required
    def get_core_metadata(core_id):
        read_cores()
        if core_id not in cores or "_DELETED" in cores[core_id]:
            abort(404)
        return filtered_core_metadata(cores[core_id])

    @app.get("/apiv1/cores/download/<string:core_id>")
    @auth.login_required
    def get_core_download(core_id: str):
        read_cores()
        if core_id not in cores or "_DELETED" in cores[core_id]:
            abort(404)
        if request.args.get("decompress") != "true":
            return send_from_directory(
                cores[core_id]["_core_dir"], core_id, as_attachment=True
            )

        (download_name, ext) = os.path.splitext(core_id)
        if ext not in decompression_methods:
            abort(415)

        core_path = os.path.join(cores[core_id]["_core_dir"], core_id)
        app.logger.debug(f"decompress requested: {core_path}")
        try:
            with open(core_path, "rb") as fp:
                process = subprocess.Popen(
                    decompression_methods[ext], stdin=fp, stdout=subprocess.PIPE
                )
                app.logger.debug("%s", process)
                return send_file(
                    process.stdout, as_attachment=True, download_name=download_name
                )
        except FileNotFoundError:
            abort(404)

    @app.delete("/apiv1/cores/delete/<string:core_id>")
    @auth.login_required
    def delete_core(core_id):
        read_cores()
        if core_id not in cores or "_DELETED" in cores[core_id]:
            abort(404)
        cores[core_id]["_DELETED"] = True
        return jsonify({})

else:

    @app.get("/apiv1/cores")
    def get_cores():
        headers = {"Authorization": request.headers.get("Authorization")}
        args = urllib.parse.urlencode(request.args)
        ret = []
        for pod_ip in get_ds_pod_ips():
            url = f"http://{pod_ip}:{app.config['DAEMONSET_PORT']}/apiv1/cores?{args}"
            app.logger.debug("GET %s", url)
            resp = requests.get(url, headers=headers)
            if not resp.ok:
                abort(resp.status_code)
            resp.encoding = "utf-8"
            ret.extend(resp.json())
        return jsonify(sorted_cores(ret))

    @app.get("/apiv1/cores/metadata/<string:node>/<string:core_id>")
    def get_node_core_metadata(node, core_id):
        pod_ip = get_ds_pod_ip(node)
        if not pod_ip:
            abort(404)
        headers = {"Authorization": request.headers.get("Authorization")}
        url = f"http://{pod_ip}:{app.config['DAEMONSET_PORT']}/apiv1/cores/metadata/{core_id}"
        app.logger.debug("GET %s", url)
        resp = requests.get(url, headers=headers)
        if not resp.ok:
            abort(resp.status_code)
        resp.encoding = "utf-8"
        return resp.json()

    @app.get("/apiv1/cores/download/<string:node>/<string:core_id>")
    def get_node_core_download(node, core_id):
        pod_ip = get_ds_pod_ip(node)
        if not pod_ip:
            abort(404)
        headers = {"Authorization": request.headers.get("Authorization")}
        args = urllib.parse.urlencode(request.args)
        url = f"http://{pod_ip}:{app.config['DAEMONSET_PORT']}/apiv1/cores/download/{core_id}?{args}"

        # Check headers from the DaemonSet server API, and pass them forward.
        #   Content-Type: application/zstd
        #   Content-Length: 19495
        #   Last-Modified: Mon, 10 Jan 2022 12:45:06 GMT
        #   Cache-Control: no-cache
        resp_headers = Headers()
        with requests.head(url, headers=headers) as resp:
            if not resp.ok:
                abort(resp.status_code)
            for k, v in resp.headers.items():
                if k in ("Date", "Server"):
                    continue
                resp_headers.add(k, v)
        if "Content-Type" not in resp_headers:
            resp_headers.add("Content-Type", "application/octet-stream")

        def stream_core():
            with requests.get(url, headers=headers, stream=True) as resp:
                if not resp.ok:
                    abort(resp.status_code)
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    yield chunk

        return app.response_class(stream_core(), headers=resp_headers)

    @app.delete("/apiv1/cores/delete/<string:node>/<string:core_id>")
    def delete_node_core(node, core_id):
        pod_ip = get_ds_pod_ip(node)
        if not pod_ip:
            abort(404)
        headers = {"Authorization": request.headers.get("Authorization")}
        url = f"http://{pod_ip}:{app.config['DAEMONSET_PORT']}/apiv1/cores/delete/{core_id}"
        app.logger.debug("DELETE %s", url)
        resp = requests.delete(url, headers=headers)
        if not resp.ok:
            abort(resp.status_code)
        resp.encoding = "utf-8"
        return resp.json()


if __name__ == "__main__":
    #
    # Run with Flask in development mode:
    # NO_TOKENS=1 FLASK_ENV=development PORT=5001 DAEMONSET=1 FAKE_K8S=1 python3 ./app.py
    # NO_TOKENS=1 FLASK_ENV=development PORT=5000 KOREDUMP_DAEMONSET_PORT=5001 DAEMONSET=0 FAKE_K8S=1 python3 ./app.py
    #
    # Run with Gunicorn in development mode:
    # NO_TOKENS=1 FLASK_ENV=development PORT=5001 DAEMONSET=1 FAKE_K8S=1 gunicorn --access-logfile=- --log-level=debug app
    # NO_TOKENS=1 FLASK_ENV=development PORT=5000 KOREDUMP_DAEMONSET_PORT=5001 DAEMONSET=0 FAKE_K8S=1 gunicorn --access-logfile=- --log-level=debug app
    #
    app.run(port=int(os.getenv("PORT")))
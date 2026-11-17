from flask import Flask, jsonify, render_template, send_from_directory, abort
from . import jobs

MAX_MB = 600


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
    jobs.mark_interrupted()

    @app.template_filter("datetimeformat")
    def _dt(ts):
        import time
        return time.strftime("%d %b %H:%M", time.localtime(ts))

    @app.errorhandler(413)
    def too_big(_):
        return render_template("form_error.html", msg=f"Upload too large (limit {MAX_MB} MB). Split it into smaller batches."), 413

    @app.route("/")
    def home():
        return render_template("index.html", recent=jobs.list_jobs(8))

    @app.route("/jobs")
    def all_jobs():
        return render_template("jobs.html", recent=jobs.list_jobs(100))

    @app.route("/jobs/<jid>")
    def job_page(jid):
        if not jobs.get(jid): abort(404)
        return render_template("job.html", jid=jid)

    @app.route("/api/jobs/<jid>")
    def job_api(jid):
        m = jobs.get(jid)
        if not m: return jsonify(error="Unknown job"), 404
        return jsonify(m)

    def _serve(sub, jid, name, attach=False):
        d = jobs.JOBS_DIR / jid / sub
        if not (d / name).is_file(): abort(404)
        return send_from_directory(d, name, as_attachment=attach)

    @app.route("/jobs/<jid>/download/<name>")
    def download(jid, name): return _serve("out", jid, name, True)

    @app.route("/jobs/<jid>/view/<name>")
    def view_file(jid, name): return _serve("out", jid, name)

    @app.route("/jobs/<jid>/image/<name>")
    def job_image(jid, name): return _serve("in", jid, name)

    from .cs1_parcels import bp as bp1
    from .cs2_compliance import bp as bp2
    from .cs3_pnl import bp as bp3
    for b in (bp1, bp2, bp3): app.register_blueprint(b)
    return app

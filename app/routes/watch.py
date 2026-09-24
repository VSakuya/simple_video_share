"""Watch blueprint: Range streaming + Drive download (P3), and comments (§13.16, §13.20)."""

from typing import Any
from flask import Blueprint, flash, redirect, render_template, request, url_for
from .. import db
from ..auth import current_user, login_required

watch_bp = Blueprint("watch", __name__, url_prefix="/watch")

#: Max comment length in characters (plain text, kaomoji allowed).
MAX_COMMENT_LENGTH = 2000

#: Kaomoji offered by the quick-insert bar on the watch page (§13.16).
KAOMOJI_SET = [
    "(≧▽≦)",
    "(≧◡≦)",
    "(≖‿≖)✧",
    "(´｡• ᵕ •｡`) ♡",
    "(•ω•)",
    "(≧ω≦)",
    "(・ω・ )",
    "(¬‿¬)",
    "(=^・ω・^=)",
    "( ˘ω˘ )",
    "o(≧v≦)o",
    "(ノ°▽°)ノ",
    "^_^",
    ">:3",
    "(T_T)",
    "(；＿；)",
]


@watch_bp.route("/<int:video_id>")
@login_required
def page(video_id: int) -> str:
    video = db.get_video_by_id(video_id)
    if video is None:
        from flask import abort
        abort(404)
    comments = db.list_comments(video_id)
    return render_template(
        "watch.html",
        video=video,
        comments=comments,
        kaomoji_set=KAOMOJI_SET,
        max_comment_length=MAX_COMMENT_LENGTH,
    )


@watch_bp.route("/<int:video_id>/comments", methods=["POST"])
@login_required
def add_comment(video_id: int) -> Any:
    """Create a comment on the video (plain text; kaomoji are just characters)."""
    video = db.get_video_by_id(video_id)
    if video is None:
        from flask import abort
        abort(404)
    me = current_user()
    assert me is not None
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Comment cannot be empty.", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    if len(body) > MAX_COMMENT_LENGTH:
        flash(f"Comment too long (max {MAX_COMMENT_LENGTH} characters).", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    # Optional parent_id for two-level replies (§13.20).
    parent_id: int | None = None
    parent_raw = request.form.get("parent_id")
    if parent_raw:
        try:
            parent_id = int(parent_raw)
        except (TypeError, ValueError):
            parent_id = None
        if parent_id is not None:
            # Validate: the parent must be a top-level comment on this video.
            parent = db.get_comment_by_id(parent_id)
            if (
                parent is None
                or parent["video_id"] != video_id
                or parent.get("parent_id") is not None
            ):
                flash("Invalid reply target.", "error")
                return redirect(url_for("watch.page", video_id=video_id))
    db.add_comment(video_id, me["id"], body, parent_id=parent_id)
    return redirect(url_for("watch.page", video_id=video_id))


@watch_bp.route("/<int:video_id>/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(video_id: int, comment_id: int) -> Any:
    """Delete a comment: the author or an admin only."""
    me = current_user()
    assert me is not None
    comment = db.get_comment_by_id(comment_id)
    if comment is None or comment["video_id"] != video_id:
        flash("Comment not found.", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    if comment["author_id"] != me["id"] and not me.get("is_admin"):
        flash("You can only delete your own comments.", "error")
        return redirect(url_for("watch.page", video_id=video_id))
    db.delete_comment(comment_id)
    flash("Comment deleted.", "success")
    return redirect(url_for("watch.page", video_id=video_id))


@watch_bp.route("/<int:video_id>/stream")
@login_required
def stream(video_id: int) -> Any:
    """Serve the cached video with HTTP Range support (P3)."""
    from flask import send_file, abort
    video = db.get_video_by_id(video_id)
    if video is None:
        abort(404)
    if not video.get("local_filename"):
        # Not cached locally; P3 will download from Drive first.
        abort(503)
    import os
    path = os.path.join(app_videos_dir(), video["local_filename"])
    if not os.path.exists(path):
        abort(503)
    db.touch_video(video_id)
    return send_file(path, conditional=True)


def app_videos_dir() -> str:
    from flask import current_app
    return str(current_app.config["VIDEOS_DIR"])

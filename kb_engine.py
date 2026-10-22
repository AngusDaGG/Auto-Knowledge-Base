"""Thin subprocess adapter: retain engine CLI while recognizing titled KB sources."""
from pathlib import Path
import sys

from knowledge_base import engine
import kb_workflow as workflow


def run(tool_dir: Path, arguments: list[str]) -> int:
    _, tool = engine(tool_dir)
    original_known = tool.knowledge_transcript
    original_completed = tool.completed_source_id

    def known(video_id, directory=None):
        legacy = original_known(video_id, directory)
        if legacy is not None:
            return legacy
        return workflow.available_source(Path(tool.ROOT), video_id, "", tool)

    def completed(source):
        try:
            key, _, canonical = workflow.identity(source, tool)
        except ValueError:
            return original_completed(source)
        path = workflow.available_source(Path(tool.ROOT), key, canonical, tool)
        if path is not None:
            return workflow.source_metadata(path, tool)["id"]
        return original_completed(source)

    tool.knowledge_transcript = known
    tool.completed_source_id = completed
    try:
        return tool.main(arguments)
    finally:
        tool.knowledge_transcript = original_known
        tool.completed_source_id = original_completed


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2:]))

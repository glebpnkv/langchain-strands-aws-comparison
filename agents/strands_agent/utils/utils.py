import ast
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands_tools.code_interpreter import AgentCoreCodeInterpreter

def _json_default(o):
    try:
        return o.__dict__
    except Exception:
        return str(o)


def extract_text(response: Any) -> str:
    try:
        msg = getattr(response, "message", None)
        if isinstance(msg, dict):
            parts = msg.get("content", [])
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
            if texts:
                return "\n".join(texts)
    except Exception:
        pass
    return str(response)


def extract_artifacts_from_sandbox(
    code_interpreter_tool: "AgentCoreCodeInterpreter",
    ci_session_name: str,
    artifacts_dir: Path,
) -> None:
    """
    Extract all files from the code interpreter sandbox to the local artifacts directory.

    Args:
        code_interpreter_tool: The code interpreter tool instance
        ci_session_name: Session name for the code interpreter
        artifacts_dir: Local directory to save artifacts to
    """
    logger = logging.getLogger(__name__)
    logger.info(f"Extracting artifacts from sandbox session '{ci_session_name}' to {artifacts_dir}")
    from strands_tools.code_interpreter.models import ExecuteCommandAction, ReadFilesAction

    try:
        # 1. Discover all files in the sandbox
        find_result = code_interpreter_tool.execute_command(
            ExecuteCommandAction(
                type="executeCommand",
                session_name=ci_session_name,
                command="find . -type f",
            )
        )

        if find_result.get("status") != "success":
            # logger.error(f"Failed to list sandbox files: {find_result}")
            print(f"Failed to list sandbox files: {find_result}")
            return

        # Extract file paths from the command output
        content = find_result.get("content", [])
        file_list_text = ""
        for item in content:
            if isinstance(item, dict) and "text" in item:
                file_list_text += ast.literal_eval(item["text"])[0].get("text")

        if not file_list_text.strip():
            # logger.info("No files found in sandbox")
            print("No files found in sandbox")
            return

        # Parse file paths (one per line, starting with ./)
        file_paths = [
            line.strip()
            for line in file_list_text.strip().split("\r\n")
            if line.strip() and not line.strip().startswith(".")
        ]

        # Also include paths that start with ./
        file_paths += [
            line.strip()
            for line in file_list_text.strip().split("\r\n")
            if line.strip() and line.strip().startswith("./")
        ]

        # Deduplicate
        file_paths = list(set(file_paths))

        if not file_paths:
            # logger.info("No files to extract from sandbox")
            print("No files to extract from sandbox")
            return

        # logger.info(f"Found {len(file_paths)} files in sandbox: {file_paths}")
        print(f"Found {len(file_paths)} files in sandbox: {file_paths}")

        # 2. Read all files from the sandbox
        read_result = code_interpreter_tool.read_files(
            ReadFilesAction(
                type="readFiles",
                session_name=ci_session_name,
                paths=file_paths,
            )
        )

        if read_result.get("status") != "success":
            # logger.error(f"Failed to read sandbox files: {read_result}")
            print(f"Failed to read sandbox files: {read_result}")
            return

        # 3. Save files to local artifacts directory
        content = read_result.get("content", [])
        saved_count = 0

        # Parse the response: content is a list with 1 dict containing "text" key
        if not content or not isinstance(content[0], dict) or "text" not in content[0]:
            print(f"Unexpected read_result format: {read_result}")
            return

        # Parse the text field into a list of items
        items = ast.literal_eval(content[0].get("text"))

        # Each item has keys: 'type' and 'resource'
        # resource has keys: 'uri' (relative path) and 'blob' (binary data)
        for item in items:
            if not isinstance(item, dict):
                continue

            resource = item.get("resource", {})
            uri = resource.get("uri", "")
            blob = resource.get("blob", "")

            if not uri:
                continue

            # Remove leading ./ if present
            clean_path = uri.replace("file:///", "").lstrip("./")
            local_file_path = artifacts_dir / clean_path

            # Create parent directories
            local_file_path.parent.mkdir(parents=True, exist_ok=True)

            # Save file content (blob is base64-encoded binary data)
            try:
                if blob:
                    # Decode base64 blob and write as binary
                    binary_data = bytes(blob)
                    local_file_path.write_bytes(binary_data)
                    print(f"Saved artifact: {clean_path}")
                    saved_count += 1
                else:
                    print(f"No content for file: {uri}")
            except Exception as e:
                print(f"Error saving file {uri}: {e}")

        # logger.info(f"Successfully extracted {saved_count} artifacts to {artifacts_dir}")
        print(f"Successfully extracted {saved_count} artifacts to {artifacts_dir}")

    except Exception as e:
        # logger.error(f"Error extracting artifacts from sandbox: {e}", exc_info=True)
        print(f"Error extracting artifacts from sandbox: {e}", exc_info=True)

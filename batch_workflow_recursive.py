#!/usr/bin/env python3
"""
batch_workflow_recursive.py

Recursively process every image inside an input directory tree through a
single-image ComfyUI workflow and write the results to a mirrored output
directory tree.

Example
-------
python batch_workflow_recursive.py \
    --workflow "Z-image超真实动漫转真人.json" \
    --input-dir  "D:/anime_images" \
    --output-dir "D:/real_images" \
    --server http://127.0.0.1:8000 \
    --skip-existing
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".webp", ".avif",
    ".bmp", ".tif", ".tiff",
)

SKIP_NODE_TYPES = {"PreviewImage"}


# ---------------------------------------------------------------------------
# JSON / workflow helpers  (ported from batch_call_single_workflow.py)
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_server(server: str) -> str:
    s = server.strip().rstrip("/")
    if not s.startswith("http://") and not s.startswith("https://"):
        s = "http://" + s
    return s


def collect_link_map(workflow: dict) -> Dict[int, list]:
    return {int(link[0]): link for link in workflow.get("links", [])}


def map_widget_values(node: dict) -> list:
    values = list(node.get("widgets_values", []))
    node_type = node.get("type", "")
    # KSampler has a UI-only "control_after_generate" item at index 1
    if node_type == "KSampler" and len(values) == 7:
        return [values[0]] + values[2:]
    return values


def workflow_to_prompt(workflow: dict) -> Dict[str, dict]:
    link_map = collect_link_map(workflow)
    prompt: Dict[str, dict] = {}

    for node in workflow.get("nodes", []):
        if node.get("type") in SKIP_NODE_TYPES:
            continue

        node_id = str(node["id"])
        class_type = node["type"]
        node_inputs: dict = {}

        # 1) Linked inputs
        for input_port in node.get("inputs", []):
            if input_port.get("type") == "IMAGEUPLOAD" or input_port.get("name") == "upload":
                continue
            link_id = input_port.get("link")
            if isinstance(link_id, int) and link_id in link_map:
                link = link_map[link_id]
                from_node = str(link[1])
                from_slot = int(link[2])
                node_inputs[input_port["name"]] = [from_node, from_slot]

        # 2) Widget-backed literal inputs (only for unlinked inputs)
        widget_values = map_widget_values(node)
        widget_index = 0
        for input_port in node.get("inputs", []):
            if input_port.get("type") == "IMAGEUPLOAD" or input_port.get("name") == "upload":
                continue
            if input_port.get("name") in node_inputs:
                continue
            if "widget" in input_port:
                if widget_index < len(widget_values):
                    node_inputs[input_port["name"]] = widget_values[widget_index]
                widget_index += 1

        prompt[node_id] = {
            "class_type": class_type,
            "inputs": node_inputs,
        }

        # Compatibility shim for ImageScaleToMaxDimension
        if class_type == "ImageScaleToMaxDimension":
            if "largest_size" not in node_inputs:
                if "max_dimension" in node_inputs:
                    node_inputs["largest_size"] = node_inputs["max_dimension"]
                elif widget_values:
                    node_inputs["largest_size"] = widget_values[-1]
                else:
                    node_inputs["largest_size"] = 1080
            if "upscale_method" not in node_inputs:
                node_inputs["upscale_method"] = "lanczos"

    return prompt


def find_node_id(workflow: dict, node_type: str) -> str:
    for node in workflow.get("nodes", []):
        if node.get("type") == node_type:
            return str(node["id"])
    raise ValueError(f"Workflow does not contain node type: {node_type}")


def collect_node_ids(workflow: dict, node_type: str) -> List[str]:
    return [
        str(node["id"])
        for node in workflow.get("nodes", [])
        if node.get("type") == node_type
    ]


def pick_best_saveimage_node_id(workflow: dict) -> str:
    save_ids = collect_node_ids(workflow, "SaveImage")
    if not save_ids:
        raise ValueError("Workflow does not contain a SaveImage node")
    if len(save_ids) == 1:
        return save_ids[0]

    id_to_node = {str(n["id"]): n for n in workflow.get("nodes", [])}
    link_map = collect_link_map(workflow)
    preferred_upstream_types = {
        "VAEDecode",
        "KSampler",
        "KSamplerAdvanced",
        "ImageUpscaleWithModel",
        "UltimateSDUpscale",
    }

    def score(save_id: str) -> Tuple[int, int]:
        node = id_to_node.get(save_id, {})
        image_link = None
        for port in node.get("inputs", []) or []:
            if port.get("name") == "images":
                image_link = port.get("link")
                break
        if not isinstance(image_link, int) or image_link not in link_map:
            return (0, int(save_id))
        from_id = str(link_map[image_link][1])
        from_type = id_to_node.get(from_id, {}).get("type", "")
        if from_type in preferred_upstream_types:
            return (3, int(save_id))
        if from_type == "LoadImage":
            return (1, int(save_id))
        return (2, int(save_id))

    return max(save_ids, key=score)


# ---------------------------------------------------------------------------
# ComfyUI API helpers
# ---------------------------------------------------------------------------

def upload_image(server: str, image_path: Path) -> str:
    with image_path.open("rb") as f:
        files = {"image": (image_path.name, f, "application/octet-stream")}
        data = {"type": "input", "overwrite": "true"}
        r = requests.post(f"{server}/upload/image", files=files, data=data, timeout=120)
        r.raise_for_status()
        payload = r.json()
    name = payload.get("name", image_path.name)
    subfolder = payload.get("subfolder", "")
    return f"{subfolder}/{name}" if subfolder else name


def queue_prompt(server: str, prompt: dict, client_id: str) -> str:
    payload = {"prompt": prompt, "client_id": client_id}
    r = requests.post(f"{server}/prompt", json=payload, timeout=120)
    if r.status_code >= 400:
        body = r.text
        try:
            body = json.dumps(r.json(), ensure_ascii=False, indent=2)
        except Exception:
            pass
        raise RuntimeError(f"/prompt failed: HTTP {r.status_code}\n{body}")
    data = r.json()
    if "prompt_id" not in data:
        raise RuntimeError(f"Unexpected /prompt response: {data}")
    return data["prompt_id"]


def wait_history(
    server: str,
    prompt_id: str,
    poll_sec: float = 0.8,
    timeout_sec: int = 3600,
) -> dict:
    start = time.time()
    while True:
        if time.time() - start > timeout_sec:
            raise TimeoutError(f"Timed out waiting for prompt {prompt_id}")
        r = requests.get(f"{server}/history/{prompt_id}", timeout=60)
        r.raise_for_status()
        data = r.json()
        if prompt_id in data:
            return data[prompt_id]
        time.sleep(poll_sec)


def extract_execution_error(history_item: dict) -> Optional[str]:
    status = history_item.get("status") or {}
    for item in status.get("messages") or []:
        if not isinstance(item, list) or len(item) < 2:
            continue
        if item[0] != "execution_error":
            continue
        p = item[1] if isinstance(item[1], dict) else {}
        node_id   = p.get("node_id", "?")
        node_type = p.get("node_type", "?")
        exc_type  = p.get("exception_type", "?")
        exc_msg   = p.get("exception_message", "")
        return f"Execution error at node {node_id} ({node_type}): {exc_type} {exc_msg}".strip()
    return None


def extract_output_images(
    history_item: dict,
    preferred_node_id: Optional[str] = None,
) -> List[dict]:
    images: List[dict] = []
    outputs = history_item.get("outputs", {})

    if preferred_node_id is not None:
        for img in (outputs.get(str(preferred_node_id), {}).get("images") or []):
            if isinstance(img, dict) and "filename" in img:
                images.append(img)
        if images:
            return images

    for node_output in outputs.values():
        for img in node_output.get("images") or []:
            if isinstance(img, dict) and "filename" in img:
                images.append(img)
    return images


def download_image(server: str, image_info: dict, out_path: Path) -> None:
    params = {
        "filename": image_info.get("filename", ""),
        "subfolder": image_info.get("subfolder", ""),
        "type": image_info.get("type", "output"),
    }
    r = requests.get(f"{server}/view", params=params, timeout=120)
    r.raise_for_status()
    out_path.write_bytes(r.content)


# ---------------------------------------------------------------------------
# Recursive file discovery
# ---------------------------------------------------------------------------

def collect_images(root: Path) -> List[Path]:
    """
    Return all image files under *root* (recursively), sorted so that files
    in the same directory are grouped together and processed in alphabetical
    order.
    """
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()          # deterministic traversal order
        for name in sorted(filenames):
            if Path(name).suffix.lower() in IMAGE_EXTENSIONS:
                found.append(Path(dirpath) / name)
    return found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively process all images in an input directory tree through "
            "a ComfyUI workflow and save the results to a mirrored output tree."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:8000",
        help="ComfyUI server address",
    )
    parser.add_argument(
        "--workflow",
        default="Z-image超真实动漫转真人.json",
        help="Path to the single-image workflow JSON file",
    )
    parser.add_argument(
        "--input-dir", required=True,
        help="Root folder containing source images (searched recursively)",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Root folder for output images (sub-directory structure is mirrored from input)",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.0,
        help="Extra delay (seconds) to insert between tasks",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="Skip input images whose output file already exists",
    )
    parser.add_argument(
        "--continue-on-error", action="store_true",
        help="Log errors and continue instead of aborting on the first failure",
    )
    parser.add_argument(
        "--timeout", type=int, default=3600,
        help="Per-image timeout in seconds while waiting for ComfyUI to finish",
    )
    args = parser.parse_args()

    server       = normalize_server(args.server)
    workflow_path = Path(args.workflow).resolve()
    input_dir    = Path(args.input_dir).resolve()
    output_dir   = Path(args.output_dir).resolve()

    # ---- Validate inputs ---------------------------------------------------
    if not workflow_path.exists():
        sys.exit(f"[ERROR] Workflow not found: {workflow_path}")
    if not input_dir.is_dir():
        sys.exit(f"[ERROR] Input directory not found: {input_dir}")

    # ---- Load & parse workflow --------------------------------------------
    workflow         = load_json(workflow_path)
    prompt_template  = workflow_to_prompt(workflow)
    load_image_id    = find_node_id(workflow, "LoadImage")
    save_image_id    = pick_best_saveimage_node_id(workflow)

    # ---- Discover images --------------------------------------------------
    all_images = collect_images(input_dir)
    if not all_images:
        sys.exit(f"[ERROR] No images found under {input_dir}")

    total = len(all_images)
    print("=" * 72)
    print(f"  Server   : {server}")
    print(f"  Workflow : {workflow_path}")
    print(f"  Input    : {input_dir}")
    print(f"  Output   : {output_dir}")
    print(f"  Images   : {total}")
    print(f"  Options  : skip-existing={args.skip_existing}  "
          f"continue-on-error={args.continue_on_error}")
    print("=" * 72)

    client_id  = str(uuid.uuid4())
    errors: List[str] = []
    skipped    = 0
    processed  = 0

    for idx, image_path in enumerate(all_images, start=1):
        # Relative path from input root  (e.g.  "subA/subB/photo.jpg")
        rel_path   = image_path.relative_to(input_dir)

        # Mirror directory structure in output
        out_subdir = output_dir / rel_path.parent
        out_subdir.mkdir(parents=True, exist_ok=True)

        # ---- Skip-existing check -----------------------------------------
        if args.skip_existing:
            # Check whether *any* output file with this stem exists
            existing = list(out_subdir.glob(f"{image_path.stem}*"))
            if existing:
                print(f"[{idx:>{len(str(total))}}/{total}] SKIP  {rel_path}")
                skipped += 1
                continue

        print(f"[{idx:>{len(str(total))}}/{total}] {rel_path}")

        try:
            # Upload source image to ComfyUI
            uploaded_name = upload_image(server, image_path)
            print(f"         uploaded  -> {uploaded_name}")

            # Deep-copy the prompt template for this image
            prompt = json.loads(json.dumps(prompt_template))

            # Point LoadImage node to the uploaded file
            prompt[load_image_id]["inputs"]["image"] = uploaded_name

            # Use the stem as the output filename prefix
            prompt[save_image_id]["inputs"]["filename_prefix"] = image_path.stem

            # Submit and wait
            prompt_id = queue_prompt(server, prompt, client_id)
            history   = wait_history(server, prompt_id, timeout_sec=args.timeout)

            # Check for execution errors reported by ComfyUI
            err = extract_execution_error(history)
            if err:
                raise RuntimeError(err)

            # Download result(s)
            out_images = extract_output_images(history, preferred_node_id=save_image_id)
            if not out_images:
                raise RuntimeError("ComfyUI returned no output images in history")

            for out_i, info in enumerate(out_images, start=1):
                suffix  = Path(info.get("filename", "out.png")).suffix or ".png"
                out_name = (
                    f"{image_path.stem}{suffix}"
                    if len(out_images) == 1
                    else f"{image_path.stem}_{out_i:02d}{suffix}"
                )
                out_path = out_subdir / out_name
                download_image(server, info, out_path)
                print(f"         saved     -> {out_path.relative_to(output_dir.parent)}")

            processed += 1

        except Exception as exc:
            msg = f"[ERROR] {rel_path}: {exc}"
            if args.continue_on_error:
                print(msg)
                errors.append(str(rel_path))
            else:
                # Re-raise so the user sees a full traceback
                raise

        if args.sleep > 0:
            time.sleep(args.sleep)

    # ---- Summary -----------------------------------------------------------
    print()
    print("=" * 72)
    print(f"  Done.  processed={processed}  skipped={skipped}  errors={len(errors)}")
    if errors:
        print("  Failed files:")
        for e in errors:
            print(f"    - {e}")
    print("=" * 72)


if __name__ == "__main__":
    main()

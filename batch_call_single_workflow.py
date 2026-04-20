import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

import requests


SKIP_NODE_TYPES = {"PreviewImage"}


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_server(server: str) -> str:
    s = server.strip().rstrip("/")
    if not s.startswith("http://") and not s.startswith("https://"):
        s = "http://" + s
    return s


def collect_link_map(workflow: dict) -> Dict[int, List]:
    return {int(link[0]): link for link in workflow.get("links", [])}


def map_widget_values(node: dict) -> List:
    values = list(node.get("widgets_values", []))
    node_type = node.get("type", "")

    # Comfy workflow JSON for KSampler often contains an extra UI-only
    # "control_after_generate" item at index 1.
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
        node_inputs = {}

        # 1) Linked inputs
        for input_port in node.get("inputs", []):
            # Frontend-only widget, not accepted by /prompt API.
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

        # Compatibility shim: some ComfyUI builds changed
        # ImageScaleToMaxDimension input names.
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


def wait_history(server: str, prompt_id: str, poll_sec: float = 0.8, timeout_sec: int = 3600) -> dict:
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


def extract_execution_error(history_item: dict) -> str | None:
    status = history_item.get("status") or {}
    messages = status.get("messages") or []
    for item in messages:
        if not isinstance(item, list) or len(item) < 2:
            continue
        if item[0] != "execution_error":
            continue
        payload = item[1] if isinstance(item[1], dict) else {}
        node_id = payload.get("node_id", "?")
        node_type = payload.get("node_type", "?")
        exc_type = payload.get("exception_type", "?")
        exc_msg = payload.get("exception_message", "")
        return f"Execution error at node {node_id} ({node_type}): {exc_type} {exc_msg}".strip()
    return None


def extract_output_images(history_item: dict, preferred_node_id: str | None = None) -> List[dict]:
    images = []
    outputs = history_item.get("outputs", {})

    # Prefer SaveImage output to avoid grabbing PreviewImage passthroughs.
    if preferred_node_id is not None:
        preferred = outputs.get(str(preferred_node_id), {})
        for img in preferred.get("images", []) or []:
            if isinstance(img, dict) and "filename" in img:
                images.append(img)
        if images:
            return images

    for node_output in outputs.values():
        for img in node_output.get("images", []) or []:
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


def build_output_dir(input_dir: Path, output_dir: Path | None) -> Path:
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir
    auto = input_dir.parent / f"#comfy{input_dir.name}"
    auto.mkdir(parents=True, exist_ok=True)
    return auto


def iter_images(input_dir: Path, exts: Tuple[str, ...]) -> List[Path]:
    files = [
        p
        for p in sorted(input_dir.iterdir())
        if p.is_file() and p.suffix.lower() in exts
    ]
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description="Use a single-image ComfyUI workflow to batch process a folder.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="ComfyUI server, e.g. http://127.0.0.1:8000")
    parser.add_argument("--workflow", default="flux klein+Anything to Real Characters.json", help="Path to single-image workflow JSON")
    parser.add_argument("--input-dir", required=True, help="Folder containing source images")
    parser.add_argument("--output-dir", default="", help="Output folder (default: sibling #comfy{input_folder_name})")
    parser.add_argument("--sleep", type=float, default=0.0, help="Delay seconds between tasks")
    args = parser.parse_args()

    server = normalize_server(args.server)
    workflow_path = Path(args.workflow).resolve()
    input_dir = Path(args.input_dir).resolve()
    output_dir = build_output_dir(input_dir, Path(args.output_dir).resolve() if args.output_dir else None)

    if not workflow_path.exists():
        raise FileNotFoundError(f"Workflow not found: {workflow_path}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input directory not found: {input_dir}")

    workflow = load_json(workflow_path)
    prompt_template = workflow_to_prompt(workflow)

    load_image_id = find_node_id(workflow, "LoadImage")
    save_image_id = find_node_id(workflow, "SaveImage")

    files = iter_images(input_dir, exts=(".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff"))
    if not files:
        raise RuntimeError(f"No input images found in {input_dir}")

    client_id = str(uuid.uuid4())
    print(f"Server: {server}")
    print(f"Workflow: {workflow_path}")
    print(f"Input: {input_dir} ({len(files)} files)")
    print(f"Output: {output_dir}")

    for idx, image_path in enumerate(files, start=1):
        print(f"[{idx}/{len(files)}] {image_path.name}")

        uploaded_name = upload_image(server, image_path)

        # Deep copy prompt template for this image
        prompt = json.loads(json.dumps(prompt_template))

        # Point LoadImage to uploaded file
        prompt[load_image_id]["inputs"]["image"] = uploaded_name
        # Make output names trace back to source file
        prompt[save_image_id]["inputs"]["filename_prefix"] = image_path.stem

        prompt_id = queue_prompt(server, prompt, client_id)
        history = wait_history(server, prompt_id)

        err = extract_execution_error(history)
        if err:
            raise RuntimeError(err)

        out_images = extract_output_images(history, preferred_node_id=save_image_id)
        if not out_images:
            print("  ! No images in history output")
            continue

        # Usually single image, but keep robust for multi-output
        for out_i, info in enumerate(out_images, start=1):
            suffix = Path(info.get("filename", "out.png")).suffix or ".png"
            if len(out_images) == 1:
                out_name = f"{image_path.stem}{suffix}"
            else:
                out_name = f"{image_path.stem}_{out_i:02d}{suffix}"
            out_path = output_dir / out_name
            download_image(server, info, out_path)
            print(f"  -> {out_path}")

        if args.sleep > 0:
            time.sleep(args.sleep)

    print("Done")


if __name__ == "__main__":
    main()

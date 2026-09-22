import os
import subprocess
import argparse
import shutil
import sys
import traceback

from aletheia_integration import build_mosaic_command

def get_relative_path(full_path, workspace_root):
    """Convert a full path to a relative path from workspace root."""
    if full_path.startswith(workspace_root):
        return full_path[len(workspace_root):].lstrip(os.sep)
    return full_path

def run_segmentation(model_type, segmentation_script, input_dir, output_dir, workspace_root):
    """Run the segmentation step."""
    print(f"Running segmentation with model type: {model_type}")
    
    script_path = os.path.abspath(segmentation_script)
    script_dir = os.path.dirname(script_path)
    input_dir_abs = os.path.abspath(input_dir)
    output_dir_abs = os.path.abspath(output_dir)
    
    # Setup environment variables to ensure unbuffered output
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'UTF-8'
    env['WORKSPACE_ROOT'] = workspace_root  # Pass workspace root to child processes
    
    command = [
            sys.executable,
            "-u",  # Unbuffered mode
            script_path,
            "--model_type", model_type,
            "--input_dir", input_dir_abs,
            "--output_dir", output_dir_abs
        ]
    print(f"Segmentation command: {subprocess.list2cmdline(command)}", flush=True)
    print(f"Segmentation cwd: {script_dir}", flush=True)
    try:
        # Use subprocess.Popen for more control over real-time output
        process = subprocess.Popen(
            command,
            cwd=script_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, 
            bufsize=1,  # Line buffered
            universal_newlines=True,
            encoding='utf-8',
            errors='replace',
            env=env
        )
        
        # Process and print output line by line in real-time
        for line in iter(process.stdout.readline, ''):
            if line.strip():
                print(line.strip(), flush=True)
                
        process.stdout.close()
        return_code = process.wait()
        
        if return_code != 0:
            print(f"Segmentation process exited with code {return_code}", flush=True)
            return False
            
    except Exception as exc:
        print(
            f"Segmentation launch failed ({type(exc).__name__}): {exc}",
            flush=True,
        )
        traceback.print_exc()
        return False
    
    print("Segmentation completed", flush=True)
    return True

def run_inpainting(in_dir, mask_dir, out_dir, checkpoint, inpainting_script, workspace_root, debug_dir=None):
    """Run the inpainting step."""
    print("Running inpainting...", flush=True)
    
    in_dir_abs = os.path.abspath(in_dir)
    mask_dir_abs = os.path.abspath(mask_dir)
    out_dir_abs = os.path.abspath(out_dir)
    checkpoint_abs = os.path.abspath(checkpoint)
    
    lama_root = os.path.dirname(os.path.dirname(os.path.abspath(inpainting_script)))
    
    env = os.environ.copy()
    env["PYTHONPATH"] = lama_root + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'UTF-8'
    env['WORKSPACE_ROOT'] = workspace_root
    
    print(f"Running from lama root: {get_relative_path(lama_root, workspace_root)}", flush=True)
    print(f"Using checkpoint: {get_relative_path(checkpoint_abs, workspace_root)}", flush=True)
    
    command = [
        sys.executable,
        "-u",
        os.path.abspath(inpainting_script),
        "--in_dir", in_dir_abs,
        "--mask_dir", mask_dir_abs,
        "--out_dir", out_dir_abs,
        "--checkpoint", checkpoint_abs
    ]

    if debug_dir:
        debug_dir_abs = os.path.abspath(debug_dir)
        command.extend(["--debug_dir", debug_dir_abs])

    try:
        print(f"Inpainting command: {subprocess.list2cmdline(command)}", flush=True)
        print(f"Inpainting cwd: {lama_root}", flush=True)
        # Use subprocess.Popen for real-time output
        process = subprocess.Popen(
            command,
            cwd=lama_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, 
            bufsize=1,  # Line buffered
            universal_newlines=True,
            encoding='utf-8',
            errors='replace',
            env=env
        )
        
        # Process and print output line by line in real-time
        for line in iter(process.stdout.readline, ''):
            if line.strip():
                # Clean path to be relative
                cleaned_line = line.strip()
                for path_fragment in [in_dir_abs, mask_dir_abs, out_dir_abs, checkpoint_abs, lama_root]:
                    if path_fragment in cleaned_line:
                        rel_path = get_relative_path(path_fragment, workspace_root)
                        cleaned_line = cleaned_line.replace(path_fragment, rel_path)
                print(cleaned_line, flush=True)
                
        process.stdout.close()
        return_code = process.wait()
        
        if return_code != 0:
            print(f"Inpainting process exited with code {return_code}", flush=True)
            return False
            
    except Exception as exc:
        print(
            f"Inpainting launch failed ({type(exc).__name__}): {exc}",
            flush=True,
        )
        traceback.print_exc()
        return False
    
    print("Inpainting completed", flush=True)
    return True


def run_mosaic(input_dir, output_dir, workspace_root):
    """Run Aletheia-Lens mode II in its isolated Python environment."""
    try:
        command = build_mosaic_command(input_dir, output_dir)
        print(f"Aletheia-Lens command: {subprocess.list2cmdline(command)}", flush=True)
        process = subprocess.Popen(
            command,
            cwd=workspace_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            encoding='utf-8',
            errors='replace',
            env={**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'UTF-8'},
        )
        for line in iter(process.stdout.readline, ''):
            if line.strip():
                print(line.rstrip(), flush=True)
        process.stdout.close()
        return_code = process.wait()
    except Exception as exc:
        print(f"Aletheia-Lens launch failed ({type(exc).__name__}): {exc}", flush=True)
        traceback.print_exc()
        return False
    if return_code != 0:
        print(f"Aletheia-Lens process exited with code {return_code}", flush=True)
        return False
    print("Aletheia-Lens mosaic repair completed", flush=True)
    return True

def main():
    parser = argparse.ArgumentParser(description="Pipeline to connect segmentation and inpainting.")
    parser.add_argument("--model_type", required=True, choices=["black_bars", "white_bars", "transparent_black", "mosaic"],
                        help="Model type for segmentation.")
    parser.add_argument("--clean_temp", action="store_true", 
                        help="Delete temporary files after processing is complete.")
    parser.add_argument("--input_dir", help="Override the base input directory.")
    parser.add_argument("--temp_dir", help="Override the segmentation/inpainting work directory.")
    parser.add_argument("--output_dir", help="Override the final image output directory.")
    args = parser.parse_args()
    
    workspace_root = os.path.dirname(os.path.abspath(__file__))
    camelia_input = os.path.abspath(args.input_dir or os.path.join(workspace_root, "camelia-decensor", "input"))
    camelia_output = os.path.abspath(args.output_dir or os.path.join(workspace_root, "camelia-decensor", "output"))
    camelia_temp = os.path.abspath(args.temp_dir or os.path.join(workspace_root, "camelia-decensor", "temp"))
    
    # Ensure temp directory exists
    os.makedirs(camelia_temp, exist_ok=True)
    
    print(f"Input directory: {get_relative_path(camelia_input, workspace_root)}")
    print(f"Output directory: {get_relative_path(camelia_output, workspace_root)}")
    print(f"Temp directory: {get_relative_path(camelia_temp, workspace_root)}")

    if args.model_type == "mosaic":
        mosaic_success = run_mosaic(
            input_dir=os.path.join(camelia_input, "mosaic"),
            output_dir=camelia_output,
            workspace_root=workspace_root,
        )
        if not mosaic_success:
            print("Mosaic repair failed. Exiting pipeline.", flush=True)
            return 1
        print("Processing complete!")
        return 0

    # Run segmentation
    segmentation_success = run_segmentation(
        model_type=args.model_type,
        segmentation_script=os.path.join(workspace_root, "smp-segmentation", "run_segmentation.py"),
        input_dir=camelia_input,
        output_dir=camelia_temp,
        workspace_root=workspace_root
    )

    if not segmentation_success:
        print("Segmentation failed. Exiting pipeline.", flush=True)
        return 1

    # Run inpainting
    inpainting_success = run_inpainting(
        in_dir=os.path.join(camelia_temp, "images"),
        mask_dir=os.path.join(camelia_temp, "masks"),
        out_dir=camelia_output,
        checkpoint=os.path.join(workspace_root, "lama-inpainting", "pretrained", "best"),
        inpainting_script=os.path.join(workspace_root, "lama-inpainting", "bin", "uncen.py"),
        workspace_root=workspace_root
    )
    
    if not inpainting_success:
        print("Inpainting failed. Exiting pipeline.", flush=True)
        return 1
    
    # Clean up temporary directory if args is set
    if args.clean_temp:
        print(f"Cleaning up temporary directory contents: {get_relative_path(camelia_temp, workspace_root)}")
        try:
            for root_dir, dirs, files in os.walk(camelia_temp):
                if root_dir == camelia_temp:
                    continue
                
                for file in files:
                    file_path = os.path.join(root_dir, file)
                    try:
                        os.unlink(file_path)
                    except Exception as e:
                        print(f"Error removing file {file_path}: {e}")
                        
                for dir_name in dirs:
                    dir_path = os.path.join(root_dir, dir_name)
                    try:
                        shutil.rmtree(dir_path)
                    except Exception as e:
                        print(f"Error removing directory {dir_path}: {e}")
            
            for file in os.listdir(camelia_temp):
                file_path = os.path.join(camelia_temp, file)
                if os.path.isfile(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
                    
            print(f"Successfully cleaned temporary directory contents while preserving the directory structure")
        except Exception as e:
            print(f"Error cleaning temporary directory contents: {e}")
            
    print("Processing complete!")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

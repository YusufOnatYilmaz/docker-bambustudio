#!/usr/bin/env python3
import sys
import subprocess
import os
import zipfile
import re
import json
import time
import uuid
import shutil
from datetime import datetime

# ------------- CLI Arguments -------------
# Usage: bambu_callback.py <stl_file_path> <request_id>
# request_id is used to create unique working directories for concurrent requests

if len(sys.argv) < 2:
    print("Usage: bambu_callback.py <stl_file_path> [request_id]", file=sys.stderr)
    sys.exit(1)

project_file = sys.argv[1]
request_id = sys.argv[2] if len(sys.argv) > 2 else str(uuid.uuid4())[:8]

# ------------- Config Paths -------------
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

# Use SYSTEM profiles from BambuStudio AppImage - these have full inheritance resolved
# Set USE_SYSTEM_PROFILES=0 to use custom configs from /config instead
USE_SYSTEM_PROFILES = os.environ.get("USE_SYSTEM_PROFILES", "1") == "1"

if USE_SYSTEM_PROFILES:
    # System profiles from BambuStudio AppImage
    SYSTEM_PROFILES_DIR = "/opt/bambustudio/resources/profiles/BBL"
    PRINTER_MACHINE_DIR = os.path.join(SYSTEM_PROFILES_DIR, "machine")
    PRINT_QUALITY_DIR = os.path.join(SYSTEM_PROFILES_DIR, "process")
    FILAMENT_CONFIG_DIR = os.path.join(SYSTEM_PROFILES_DIR, "filament")
else:
    # Custom config files from /config directory
    PRINTER_MACHINE_DIR = os.path.join(CONFIG_DIR, "process_config")
    PRINT_QUALITY_DIR = os.path.join(CONFIG_DIR, "printer_config")
    FILAMENT_CONFIG_DIR = os.path.join(CONFIG_DIR, "filament_config")

# Default config files (can be overridden via environment variables)
DEFAULT_PRINTER_MACHINE = "Bambu Lab X1 Carbon 0.4 nozzle.json"
DEFAULT_PRINT_QUALITY = "0.20mm Standard @BBL X1C.json"
DEFAULT_FILAMENT_CONFIG = "Bambu PLA Basic @BBL X1C.json"

# Get config file names from environment (just filename, not full path)
printer_machine_name = os.environ.get("PRINTER_MACHINE_NAME", DEFAULT_PRINTER_MACHINE)
print_quality_name = os.environ.get("PRINT_QUALITY_NAME", DEFAULT_PRINT_QUALITY)
filament_config_name = os.environ.get("FILAMENT_CONFIG_NAME", DEFAULT_FILAMENT_CONFIG)

# Support settings - ALWAYS ENABLED
# Support type: "normal(auto)", "tree(auto)", "hybrid(auto)"
# Note: If the model doesn't need supports, BambuStudio won't generate any
ENABLE_SUPPORT = "1"  # Always enabled
SUPPORT_TYPE = "tree(auto)"  # Tree supports for best quality

# Build full paths
printer_machine_config = os.path.join(PRINTER_MACHINE_DIR, printer_machine_name)
print_quality_config = os.path.join(PRINT_QUALITY_DIR, print_quality_name)
filament_config = os.path.join(FILAMENT_CONFIG_DIR, filament_config_name)

# ------------- Concurrency-Safe Working Directory -------------
# Each request gets its own unique working directory to avoid conflicts
WORK_BASE_DIR = os.path.join(CONFIG_DIR, "work")
request_work_dir = os.path.join(WORK_BASE_DIR, request_id)
output_directory = os.path.join(request_work_dir, "slice_output")

# Determine if input is STL or 3MF
input_extension = os.path.splitext(project_file)[1].lower()
is_3mf_file = input_extension == ".3mf"

def check_3mf_has_slice_data(filepath):
    """
    Check if a 3MF file contains slice data (is pre-sliced).
    Returns True if slice data exists, False otherwise.
    
    Pre-sliced 3MF files from BambuStudio contain:
    - Metadata/slice_info.config (filament usage data)
    - Metadata/plate_N.gcode (G-code for each plate)
    """
    try:
        with zipfile.ZipFile(filepath, 'r') as zf:
            file_list = zf.namelist()
            # Check for slice metadata files
            has_slice_info = any(f.endswith('slice_info.config') for f in file_list)
            has_gcode = any(f.endswith('.gcode') and 'plate' in f.lower() for f in file_list)
            return has_slice_info and has_gcode
    except Exception:
        return False

# Check if 3MF is pre-sliced or needs slicing
is_presliced_3mf = False
if is_3mf_file:
    is_presliced_3mf = check_3mf_has_slice_data(project_file)

# needs_slicing is True for STL files OR for 3MF files without slice data
needs_slicing = not is_presliced_3mf

# Generate unique output filename in the request's work directory
input_basename = os.path.splitext(os.path.basename(project_file))[0]
if is_presliced_3mf:
    # For pre-sliced 3mf, use the input file directly for parsing
    min_save_3mf = project_file
else:
    # For STL or un-sliced 3MF input, generate output 3mf path
    min_save_3mf = os.path.join(request_work_dir, f"{input_basename}_output.3mf")

slicer_timeout_secs = int(os.environ.get("BAMBU_SLICE_TIMEOUT", "120"))
debug_log_path = os.path.join(request_work_dir, "debug.log")

# Cleanup config: auto-delete work dirs older than this (seconds)
CLEANUP_AGE_SECONDS = int(os.environ.get("CLEANUP_AGE_SECONDS", "3600"))  # 1 hour default

bambu_slicer_exe = "/opt/bambustudio/AppRun"

# ------------- Helper Functions -------------
def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{request_id}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(debug_log_path), exist_ok=True)
        with open(debug_log_path, "a", encoding="utf-8") as lf:
            lf.write(line + "\n")
    except Exception:
        pass

def file_info(path):
    if os.path.exists(path):
        try:
            return f"exists,size={os.path.getsize(path)}"
        except Exception:
            return "exists,size=?"
    return "missing"

def cleanup_old_work_dirs():
    """Remove work directories older than CLEANUP_AGE_SECONDS"""
    if not os.path.exists(WORK_BASE_DIR):
        return

    now = time.time()
    cleaned = 0

    try:
        for dirname in os.listdir(WORK_BASE_DIR):
            dirpath = os.path.join(WORK_BASE_DIR, dirname)
            if not os.path.isdir(dirpath):
                continue

            try:
                # Check directory age by modification time
                mtime = os.path.getmtime(dirpath)
                age = now - mtime

                if age > CLEANUP_AGE_SECONDS:
                    shutil.rmtree(dirpath, ignore_errors=True)
                    cleaned += 1
            except Exception:
                pass

        if cleaned > 0:
            log(f"Cleanup: removed {cleaned} old work directories")
    except Exception as e:
        log(f"Cleanup error: {e}")

def cleanup_current_work_dir():
    """Remove the current request's work directory"""
    try:
        if os.path.exists(request_work_dir):
            shutil.rmtree(request_work_dir, ignore_errors=True)
            log(f"Cleaned up work directory: {request_work_dir}")
    except Exception as e:
        log(f"Failed to cleanup work directory: {e}")

# ------------- Main Execution -------------
start_time = time.time()

# Run cleanup of old directories before starting
cleanup_old_work_dirs()

# Create fresh work directory for this request
os.makedirs(request_work_dir, exist_ok=True)
os.makedirs(output_directory, exist_ok=True)

log("=== START bambu_callback.py ===")
log(f"request_id={request_id}")
log(f"python version: {sys.version}")
log(f"cwd={os.getcwd()} user={os.geteuid() if hasattr(os, 'geteuid') else 'n/a'}")

# Log config info
log(f"Config directories:")
log(f"  printer_machine_dir: {PRINTER_MACHINE_DIR}")
log(f"  print_quality_dir: {PRINT_QUALITY_DIR}")
log(f"  filament_config_dir: {FILAMENT_CONFIG_DIR}")
log(f"  work_dir: {request_work_dir}")

log(f"Check inputs:")
log(f"  project_file: {project_file} -> {file_info(project_file)}")
log(f"  input_type: {'3MF (pre-sliced)' if is_presliced_3mf else '3MF (needs slicing)' if is_3mf_file else 'STL (needs slicing)'}")
log(f"  printer_machine_config: {printer_machine_config} -> {file_info(printer_machine_config)}")
log(f"  print_quality_config: {print_quality_config} -> {file_info(print_quality_config)}")
log(f"  filament_config: {filament_config} -> {file_info(filament_config)}")
log(f"  output_directory: {output_directory} -> {file_info(output_directory)}")
log(f"  enable_support: {ENABLE_SUPPORT}, support_type: {SUPPORT_TYPE}")

# ------------- Create Modified Process Config -------------
# Create a modified process config with:
# - Supports enabled (always for models that need slicing)
# - Custom wall_loops and sparse_infill_density if provided via environment

actual_process_config = print_quality_config

# Get optional slicing parameters from environment
WALL_LOOPS = os.environ.get("WALL_LOOPS", "").strip()
INFILL_DENSITY = os.environ.get("INFILL_DENSITY", "").strip()

# Check if we need to modify the process config
needs_config_modification = needs_slicing and (
    ENABLE_SUPPORT == "1" or WALL_LOOPS or INFILL_DENSITY
)

if needs_config_modification:
    log("Creating modified process config...")
    try:
        # Read the original process config
        with open(print_quality_config, 'r', encoding='utf-8') as f:
            process_data = json.load(f)

        # Add/override support settings
        if ENABLE_SUPPORT == "1":
            process_data["enable_support"] = "1"
            process_data["support_type"] = SUPPORT_TYPE
            process_data["support_threshold_angle"] = os.environ.get("SUPPORT_THRESHOLD_ANGLE", "30")
            log(f"  - Supports: enabled ({SUPPORT_TYPE})")

        # Add/override wall loops if provided
        if WALL_LOOPS:
            process_data["wall_loops"] = WALL_LOOPS
            log(f"  - Wall loops: {WALL_LOOPS}")

        # Add/override infill density if provided
        if INFILL_DENSITY:
            # Ensure it ends with % if not already
            density_value = INFILL_DENSITY if INFILL_DENSITY.endswith('%') else f"{INFILL_DENSITY}%"
            process_data["sparse_infill_density"] = density_value
            log(f"  - Infill density: {density_value}")

        # Write to a temporary file in the work directory
        modified_process_path = os.path.join(request_work_dir, "process_modified.json")
        with open(modified_process_path, 'w', encoding='utf-8') as f:
            json.dump(process_data, f, indent=4)

        actual_process_config = modified_process_path
        log(f"  Created modified process config: {modified_process_path}")
        
        # Debug: Log ALL keys in config to see what we have
        log(f"  DEBUG - All keys in config ({len(process_data)} total):")
        log(f"    {list(process_data.keys())}")
    except Exception as e:
        log(f"  WARNING: Failed to create modified process config: {e}")
        log(f"  Falling back to original process config")

# ------------- Slicing (if needed) -------------
if not needs_slicing:
    log("=== Skipping slicing (input is pre-sliced 3MF) ===")
else:
    # Use xvfb-run to provide a virtual display for BambuStudio
    # BambuStudio requires X11 even in CLI mode
    # System profiles are patched in Dockerfile to include nozzle_volume_type
    # Official syntax: bambu-studio [ OPTIONS ] [ file.3mf/file.stl ... ]

    # Auto-orient and arrange settings
    # --orient: For STL files, enable auto-orient for best printability
    #           For 3MF files, disable it - they usually have correct orientation already
    #           (also avoids Voronoi vertex errors with complex 3MF geometry)
    # --arrange: Always enable to center model on bed
    auto_orient = "0" if is_3mf_file else "1"  # Disable for 3MF, enable for STL
    auto_arrange = "1"  # Always enabled

    slice_command = [
        "xvfb-run",
        "--auto-servernum",
        "--server-args=-screen 0 1024x768x24",
        bambu_slicer_exe,
        # Auto-orient and arrange for best printability
        f"--orient={auto_orient}",
        f"--arrange={auto_arrange}",
        # Load system profiles (patched with nozzle_volume_type in Dockerfile)
        # Uses actual_process_config which may be modified to enable supports
        f"--load-settings={printer_machine_config};{actual_process_config}",
        f"--load-filaments={filament_config}",
        f"--export-3mf={min_save_3mf}",
        f"--export-slicedata={output_directory}",
        f"--export-settings={output_directory}/settings.json",
        "--slice=1",
        "--min-save",
        project_file,  # Input file comes LAST per official docs
    ]

    log(f"Slicer executable: {bambu_slicer_exe} -> {file_info(bambu_slicer_exe)}")
    log("Slice command:")
    log("  " + " ".join(f'"{c}"' if " " in c else c for c in slice_command))
    log(f"Slicer timeout: {slicer_timeout_secs}s")

# ------------- Run slicer (if needed) -------------
if needs_slicing:
    # ------------- Enhanced Debugging: Validate X11/xvfb -------------
    log("=== X11/xvfb validation ===")
    try:
        xvfb_test = subprocess.run(
            ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1024x768x24", "xdpyinfo"],
            capture_output=True, text=True, timeout=10
        )
        if xvfb_test.returncode == 0:
            log("xvfb validation: OK (xdpyinfo succeeded)")
        else:
            log(f"xvfb validation: WARN returncode={xvfb_test.returncode}")
            log(f"  stderr: {xvfb_test.stderr[:500] if xvfb_test.stderr else 'none'}")
    except FileNotFoundError:
        log("xvfb validation: FAIL - xvfb-run or xdpyinfo not found!")
    except subprocess.TimeoutExpired:
        log("xvfb validation: FAIL - xdpyinfo timed out (X server issue)")
    except Exception as ex:
        log(f"xvfb validation: ERROR - {ex}")

    # ------------- Enhanced Debugging: Check OpenGL/Mesa -------------
    log("=== OpenGL/Mesa validation ===")
    try:
        glxinfo_test = subprocess.run(
            ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1024x768x24", "glxinfo", "-B"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"}
        )
        if glxinfo_test.returncode == 0:
            log("OpenGL validation: OK")
            # Extract renderer info
            for line in glxinfo_test.stdout.splitlines()[:10]:
                if "renderer" in line.lower() or "vendor" in line.lower() or "version" in line.lower():
                    log(f"  {line.strip()}")
        else:
            log(f"OpenGL validation: WARN returncode={glxinfo_test.returncode}")
            log(f"  stderr: {glxinfo_test.stderr[:300] if glxinfo_test.stderr else 'none'}")
    except FileNotFoundError:
        log("OpenGL validation: SKIP - glxinfo not installed (optional)")
    except subprocess.TimeoutExpired:
        log("OpenGL validation: FAIL - glxinfo timed out")
    except Exception as ex:
        log(f"OpenGL validation: ERROR - {ex}")

    # ------------- Run slicer with real-time monitoring -------------
    import threading

    process_alive = True

    def monitor_output():
        """Monitor process output in real-time"""
        global process_alive
        last_log_time = time.time()

        while process_alive:
            # Check for output every 0.5 seconds
            time.sleep(0.5)
            elapsed = time.time() - t0

            # Log progress every 10 seconds
            if time.time() - last_log_time >= 10:
                log(f"  ... slicer running ({elapsed:.0f}s elapsed)")
                last_log_time = time.time()

                # Check if output files are being created
                if os.path.exists(min_save_3mf):
                    log(f"  ... 3mf file detected: {file_info(min_save_3mf)}")
                if os.path.exists(output_directory):
                    try:
                        files = os.listdir(output_directory)
                        if files:
                            log(f"  ... output dir has {len(files)} files: {files[:5]}")
                    except Exception:
                        pass

    log("=== Starting slicer process ===")
    try:
        t0 = time.time()

        # Use Popen for real-time monitoring
        # Set environment to force X11 and disable Wayland for GLFW (BambuStudio V2.2.x)
        slicer_env = {
            **os.environ,
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "MESA_DEBUG": "silent",
            # Force X11 for GLFW, completely disable Wayland
            "XDG_SESSION_TYPE": "x11",
            "WAYLAND_DISPLAY": "",
            "GDK_BACKEND": "x11",
            "GLFW_IM_MODULE": "",
            "SDL_VIDEODRIVER": "x11",
        }
        process = subprocess.Popen(
            slice_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            env=slicer_env
        )

        log(f"Slicer PID: {process.pid}")

        # Start monitoring thread
        monitor_thread = threading.Thread(target=monitor_output, daemon=True)
        monitor_thread.start()

        try:
            stdout, stderr = process.communicate(timeout=slicer_timeout_secs)
            process_alive = False

            dt = time.time() - t0

            if process.returncode == 0:
                log(f"Slicer finished: returncode=0 duration={dt:.2f}s")
                log(f"Slicer stdout len={len(stdout)} stderr len={len(stderr)}")

                # Log last few lines of output for debugging
                if stdout:
                    log("Slicer stdout (last 500 chars):")
                    for line in stdout[-500:].splitlines()[-10:]:
                        log(f"  {line}")

                # Write output to work directory
                try:
                    with open(os.path.join(request_work_dir, "bambu_cli_output.txt"), "w", encoding="utf-8") as log_file:
                        log_file.write(stdout or "")
                        log_file.write("\n--- STDERR ---\n")
                        log_file.write(stderr or "")
                    log("Wrote bambu_cli_output.txt")
                except Exception as e:
                    log(f"Failed writing bambu_cli_output.txt: {e}")
            else:
                log(f"ERROR: slicer failed returncode={process.returncode}")
                log(f"Slicer stdout (last 1000 chars): {stdout[-1000:] if stdout else 'none'}")
                log(f"Slicer stderr (last 1000 chars): {stderr[-1000:] if stderr else 'none'}")

                try:
                    with open(os.path.join(request_work_dir, "bambu_cli_error.txt"), "w", encoding="utf-8") as error_file:
                        error_file.write(f"Error: returncode={process.returncode}\n")
                        error_file.write("Standard Output:\n" + (stdout or "") + "\n")
                        error_file.write("Standard Error:\n" + (stderr or "") + "\n")
                    log("Wrote bambu_cli_error.txt")
                except Exception as e2:
                    log(f"Failed writing bambu_cli_error.txt: {e2}")
                cleanup_current_work_dir()
                sys.exit(1)

        except subprocess.TimeoutExpired:
            process_alive = False
            log(f"ERROR: slicer TIMEOUT after {slicer_timeout_secs}s")

            # Try to get partial output
            try:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""

            log(f"Partial stdout (last 1000 chars): {stdout[-1000:] if stdout else 'none'}")
            log(f"Partial stderr (last 1000 chars): {stderr[-1000:] if stderr else 'none'}")

            # Check what files were created before timeout
            log("Files created before timeout:")
            log(f"  3mf: {min_save_3mf} -> {file_info(min_save_3mf)}")
            log(f"  output_dir: {output_directory} -> {file_info(output_directory)}")
            try:
                if os.path.exists(output_directory):
                    for f in os.listdir(output_directory):
                        log(f"    {f}: {file_info(os.path.join(output_directory, f))}")
            except Exception:
                pass

            try:
                with open(os.path.join(request_work_dir, "bambu_cli_error.txt"), "w", encoding="utf-8") as errf:
                    errf.write(f"Timeout after {slicer_timeout_secs}s\n")
                    errf.write(f"Partial STDOUT:\n{stdout or 'none'}\n")
                    errf.write(f"Partial STDERR:\n{stderr or 'none'}\n")
            except Exception as e2:
                log(f"Failed writing bambu_cli_error.txt: {e2}")

            cleanup_current_work_dir()
            sys.exit(1)

    except Exception as e:
        process_alive = False
        log(f"ERROR: Unexpected exception: {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())
        cleanup_current_work_dir()
        sys.exit(1)

    # After slicing: verify outputs
    log("Post-slice output check:")
    log(f"  3mf: {min_save_3mf} -> {file_info(min_save_3mf)}")
    log(f"  slicedata dir: {output_directory} -> {file_info(output_directory)}")
log(f"  settings.json: {os.path.join(output_directory,'settings.json')} -> {file_info(os.path.join(output_directory,'settings.json'))}")

used_g_pattern = re.compile(r'used_g="([^"]+)"')
used_m_pattern = re.compile(r'used_m="([^"]+)"')
used_g_value = None
used_m_value = None
printing_time_line = None

# Default filament density (PLA = 1.24 g/cm³) for calculating weight from length
# Formula: weight(g) = length(m) * (π * (diameter/2)² cm²) * density(g/cm³)
# For 1.75mm filament: cross-section area = π * (0.175/2)² = 0.02405 cm²
DEFAULT_FILAMENT_DENSITY = 1.24  # g/cm³ for PLA
FILAMENT_DIAMETER_CM = 0.175  # 1.75mm in cm
FILAMENT_CROSS_SECTION = 3.14159 * (FILAMENT_DIAMETER_CM / 2) ** 2  # cm²

# Parse 3mf contents
if not os.path.exists(min_save_3mf):
    log("ERROR: .3mf file missing, cannot parse; exiting.")
    cleanup_current_work_dir()
    sys.exit(1)

log("Opening .3mf zip to parse metadata...")
try:
    with zipfile.ZipFile(min_save_3mf, 'r') as zf:
        file_list = zf.namelist()
        log(f".3mf contains {len(file_list)} files")
        for file_name in file_list:
            if file_name == "Metadata/slice_info.config":
                log("Reading Metadata/slice_info.config")
                with zf.open(file_name) as extracted_file:
                    try:
                        content = extracted_file.read().decode('utf-8', errors='ignore')
                        for line in content.splitlines():
                            if line.strip().startswith('<filament'):
                                # Try to get used_g first
                                match_g = used_g_pattern.search(line)
                                if match_g:
                                    used_g_value = match_g.group(1)
                                    log(f"Parsed used_g={used_g_value}")
                                # Also get used_m as fallback
                                match_m = used_m_pattern.search(line)
                                if match_m:
                                    used_m_value = match_m.group(1)
                                    log(f"Parsed used_m={used_m_value}")
                    except Exception as e:
                        log(f"Failed parsing slice_info.config: {e}")

            if file_name == "Metadata/plate_1.gcode":
                log("Reading Metadata/plate_1.gcode (header lines)")
                with zf.open(file_name) as extracted_file:
                    try:
                        content = extracted_file.read().decode('utf-8', errors='ignore')
                        lines = content.splitlines()
                        if len(lines) >= 3:
                            printing_time_line = lines[2]
                            log(f"printing_time_line='{printing_time_line[:120]}'")
                    except Exception as e:
                        log(f"Failed parsing plate_1.gcode: {e}")

except zipfile.BadZipFile as e:
    log(f"ERROR: BadZipFile for {min_save_3mf}: {e}")
    cleanup_current_work_dir()
    sys.exit(1)

# Compute time
if printing_time_line:
    match = re.search(r'total estimated time: ((?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?)', printing_time_line)
    if match:
        days = int(match.group(2)) if match.group(2) else 0
        hours = int(match.group(3)) if match.group(3) else 0
        minutes = int(match.group(4)) if match.group(4) else 0
        seconds = int(match.group(5)) if match.group(5) else 0
        Total_printing_hours = days * 24 + hours + minutes / 60 + seconds / 3600
    else:
        Total_printing_hours = 0
else:
    Total_printing_hours = 0
log(f"Total_printing_hours={Total_printing_hours:.3f}")

def calculate_cost_per_hour(item_name, cost, lifespan_hours):
    return item_name, round(cost / lifespan_hours, 3)

items = [
    {"name": "Nozzle", "price": 10, "lifespan_min": 300},
    {"name": "PTFE Tube (Hotend Liner)", "price": 5, "lifespan_min": 500},
    {"name": "Extruder Gears", "price": 20, "lifespan_min": 1000},
    {"name": "Build Plate Surface", "price": 30, "lifespan_min": 500},
    {"name": "Cooling Fans", "price": 15, "lifespan_min": 2000},
    {"name": "Belts", "price": 10, "lifespan_min": 2000},
    {"name": "Linear Bearings and Rods", "price": 40, "lifespan_min": 3000},
    {"name": "Hotend Heater Cartridge", "price": 15, "lifespan_min": 2000},
    {"name": "Thermistor", "price": 10, "lifespan_min": 2000},
    {"name": "Drive Gears/Pulleys", "price": 25, "lifespan_min": 3000},
    {"name": "Motherboard", "price": 150, "lifespan_min": 5000},
    {"name": "Stepper Driver", "price": 20, "lifespan_min": 5000},
    {"name": "LCD Screen", "price": 60, "lifespan_min": 5*365*24},
    {"name": "Power Supply Unit (PSU)", "price": 80, "lifespan_min": 5000},
    {"name": "Stepper Motors", "price": 30, "lifespan_min": 10000},
    {"name": "Filament Sensor", "price": 15, "lifespan_min": 3000},
    {"name": "Print Bed Heating Element", "price": 50, "lifespan_min": 5000},
]
total_dep_cost_per_hour = 0
for item in items:
    name, cost_per_hour = calculate_cost_per_hour(item["name"], item["price"], item["lifespan_min"])
    total_dep_cost_per_hour += cost_per_hour
log(f"Depreciation cost per hour total={total_dep_cost_per_hour:.3f}")

def calculate_electricity_cost(power_watts, electricity_rate_per_kwh):
    return round((power_watts / 1000) * electricity_rate_per_kwh, 3)

power_consumption_watts = 105
electricity_rate_per_kwh = 0.06
electricity_cost_per_hour = calculate_electricity_cost(power_consumption_watts, electricity_rate_per_kwh)
log(f"Electricity cost per hour={electricity_cost_per_hour:.3f}")

def calculate_filament_cost(filament_price, spool_weight, total_used_grams):
    return round((filament_price / spool_weight) * total_used_grams, 2)

# Get filament price from environment (passed from order item)
filament_price = float(os.environ.get("FILAMENT_PRICE", "12"))
spool_weight = 1000

# Calculate total used grams - prefer used_g but fall back to calculating from used_m
total_used_grams = float(used_g_value) if used_g_value else 0

# If used_g is 0 or not available, calculate from used_m (meters)
# BambuStudio V2.2.x CLI doesn't resolve filament density inheritance, so used_g is often 0
if total_used_grams == 0 and used_m_value:
    used_meters = float(used_m_value)
    # Calculate weight: length(m) * 100(cm/m) * cross_section(cm²) * density(g/cm³)
    total_used_grams = used_meters * 100 * FILAMENT_CROSS_SECTION * DEFAULT_FILAMENT_DENSITY
    log(f"Calculated used_g from used_m: {used_meters}m * {FILAMENT_CROSS_SECTION:.5f}cm² * {DEFAULT_FILAMENT_DENSITY}g/cm³ = {total_used_grams:.2f}g")

filament_cost = calculate_filament_cost(filament_price, spool_weight, total_used_grams)
log(f"Filament price={filament_price} used_g={total_used_grams:.2f} -> filament_cost={filament_cost:.2f}")

total_depreciation_cost = Total_printing_hours * total_dep_cost_per_hour
total_electricity_cost = Total_printing_hours * electricity_cost_per_hour
total_cost = total_depreciation_cost + total_electricity_cost + filament_cost

# Output result as JSON to stdout for easy parsing by Node.js
result_json = {
    "request_id": request_id,
    "file": os.path.basename(project_file),
    "total_cost": round(total_cost, 4),
    "printing_hours": round(Total_printing_hours, 3),
    "filament_grams": round(total_used_grams, 2),
    "filament_cost": round(filament_cost, 2),
    "depreciation_cost": round(total_depreciation_cost, 4),
    "electricity_cost": round(total_electricity_cost, 4),
    "config": {
        "printerMachine": printer_machine_name,
        "printQuality": print_quality_name,
        "filament": filament_config_name
    }
}

# Print JSON result (this is what server.js parses)
print(f"RESULT_JSON:{json.dumps(result_json)}", flush=True)

# Also print legacy format for backwards compatibility
print(f"Total Cost: {total_cost:.4f}", flush=True)
log(f"Total Cost: {total_cost:.4f}")

# Cleanup work directory after successful completion
cleanup_current_work_dir()

elapsed = time.time() - start_time
log(f"=== END bambu_callback.py (elapsed {elapsed:.2f}s) ===")

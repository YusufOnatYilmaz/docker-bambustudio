// server.js - Headless BambuStudio API
const express = require("express");
const cors = require("cors");
const fs = require("fs");
const fsp = fs.promises;
const path = require("path");
const crypto = require("crypto");
const { execFile } = require("child_process");
const helmet = require("helmet");
const morgan = require("morgan");
const rateLimit = require("express-rate-limit");

// ------------ Config (env-driven) ------------
const PORT = parseInt(process.env.PORT || "8080", 10);
const CONFIG_DIR = "/config";
const WORK_DIR = path.join(CONFIG_DIR, "work");
const PYTHON_SCRIPT = "/app/bambu_callback.py";
const BAMBU_BIN = "/opt/bambustudio/AppRun";

// Use SYSTEM profiles from BambuStudio AppImage - these have full inheritance resolved
// Set USE_SYSTEM_PROFILES=0 to use custom configs from /config instead
const USE_SYSTEM_PROFILES = process.env.USE_SYSTEM_PROFILES !== "0";

const SYSTEM_PROFILES_DIR = "/opt/bambustudio/resources/profiles/BBL";
const PRINTER_MACHINE_DIR = USE_SYSTEM_PROFILES
  ? path.join(SYSTEM_PROFILES_DIR, "machine")
  : path.join(CONFIG_DIR, "process_config");
const PRINT_QUALITY_DIR = USE_SYSTEM_PROFILES
  ? path.join(SYSTEM_PROFILES_DIR, "process")
  : path.join(CONFIG_DIR, "printer_config");
const FILAMENT_CONFIG_DIR = USE_SYSTEM_PROFILES
  ? path.join(SYSTEM_PROFILES_DIR, "filament")
  : path.join(CONFIG_DIR, "filament_config");

const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || "").split(",").map(s => s.trim()).filter(Boolean);
const MAX_FILES = parseInt(process.env.MAX_FILES || "8", 10);
const MAX_FILE_BYTES = parseInt(process.env.MAX_FILE_BYTES || (10 * 1024 * 1024).toString(), 10);
const BODY_LIMIT = process.env.BODY_LIMIT || "100mb";

// Default config files (using X1 Carbon as default)
const DEFAULT_PRINTER_MACHINE = "Bambu Lab X1 Carbon 0.4 nozzle.json";
const DEFAULT_PRINT_QUALITY = "0.20mm Standard @BBL X1C.json";
const DEFAULT_FILAMENT_CONFIG = "Bambu PLA Basic @BBL X1C.json";

// ------------ App init ------------
const app = express();
app.set("x-powered-by", false);
app.use(helmet({ contentSecurityPolicy: false }));
app.use(morgan("combined"));
app.use(express.json({ limit: BODY_LIMIT }));

// CORS allow-list
const corsOpts = {
  origin: (origin, cb) => {
    if (!origin || ALLOWED_ORIGINS.includes(origin) || ALLOWED_ORIGINS.includes("*")) return cb(null, true);
    return cb(new Error("CORS blocked"), false);
  },
  credentials: false,
};
app.use(ALLOWED_ORIGINS.length ? cors(corsOpts) : cors({ origin: false }));

// Rate limit
const limiter = rateLimit({
  windowMs: 60 * 1000,
  max: parseInt(process.env.RATE_LIMIT_MAX || "60", 10),
  standardHeaders: true,
  legacyHeaders: false,
});
app.use(limiter);

// Ensure dirs exist
fs.mkdirSync(WORK_DIR, { recursive: true });

// ------------ Utilities ------------
function generateRequestId() {
  return crypto.randomBytes(8).toString("hex");
}

function sanitizeFilename(name) {
  const base = path.basename(name).replace(/[^a-zA-Z0-9._-]/g, "_");
  const lowerBase = base.toLowerCase();
  if (!lowerBase.endsWith(".stl") && !lowerBase.endsWith(".3mf")) {
    throw new Error("Only .stl and .3mf files are allowed");
  }
  return base;
}

function sanitizeConfigName(name) {
  if (!name) return null;
  // Only allow safe characters for config filenames
  const safe = path.basename(name).replace(/[^a-zA-Z0-9._@ -]/g, "_");
  if (!safe.toLowerCase().endsWith(".json")) {
    throw new Error("Config file must be .json");
  }
  return safe;
}

async function configFileExists(dir, filename) {
  if (!filename) return false;
  const fullPath = path.join(dir, filename);
  try {
    await fsp.access(fullPath, fs.constants.R_OK);
    return true;
  } catch {
    return false;
  }
}

function runPython(stlPath, requestId, env = {}) {
  return new Promise((resolve, reject) => {
    execFile("python3", [PYTHON_SCRIPT, stlPath, requestId], {
      env: { ...process.env, ...env },
      timeout: parseInt(process.env.BAMBU_SLICE_TIMEOUT || "300", 10) * 1000,
      maxBuffer: 10 * 1024 * 1024,
    }, (err, stdout, stderr) => {
      if (err) {
        err.stderr = stderr;
        err.stdout = stdout;
        return reject(err);
      }
      resolve({ stdout, stderr });
    });
  });
}

function parseResultJson(stdout) {
  // Look for RESULT_JSON:{...} line in output
  const match = stdout.match(/RESULT_JSON:(\{.*\})/);
  if (match) {
    try {
      return JSON.parse(match[1]);
    } catch (e) {
      console.error("Failed to parse RESULT_JSON:", e);
    }
  }
  return null;
}

// ------------ Health & Check endpoints ------------

// Basic liveness check
app.get("/healthz", (_req, res) => res.json({ status: "ok" }));

// System check - validates system dependencies (xvfb, opengl, binaries)
// Does NOT check config files - that's files_check's job
app.get("/system_check", async (_req, res) => {
  const checks = {
    bambu_binary: { status: "pending" },
    python_script: { status: "pending" },
    config_writable: { status: "pending" },
    xvfb: { status: "pending" },
    opengl: { status: "pending" },
  };

  try {
    // Check BambuStudio binary
    if (fs.existsSync(BAMBU_BIN)) {
      checks.bambu_binary = { status: "ok", path: BAMBU_BIN };
    } else {
      checks.bambu_binary = { status: "fail", error: "binary not found" };
    }

    // Check Python script
    if (fs.existsSync(PYTHON_SCRIPT)) {
      checks.python_script = { status: "ok", path: PYTHON_SCRIPT };
    } else {
      checks.python_script = { status: "fail", error: "script not found" };
    }

    // Check config directory is writable
    try {
      const testFile = path.join(CONFIG_DIR, ".rw_test");
      await fsp.writeFile(testFile, "ok");
      await fsp.unlink(testFile);
      checks.config_writable = { status: "ok", path: CONFIG_DIR };
    } catch (e) {
      checks.config_writable = { status: "fail", error: e.message };
    }

    // Check xvfb (X Virtual Framebuffer)
    try {
      await new Promise((resolve, reject) => {
        execFile("xvfb-run", ["--auto-servernum", "--server-args=-screen 0 1024x768x24", "xdpyinfo"], {
          timeout: 10000,
          env: { ...process.env, LIBGL_ALWAYS_SOFTWARE: "1" },
        }, (err, stdout, stderr) => {
          if (err) reject(err);
          else resolve({ stdout, stderr });
        });
      });
      checks.xvfb = { status: "ok", message: "xvfb-run working" };
    } catch (e) {
      checks.xvfb = { status: "fail", error: e.message || "xvfb test failed" };
    }

    // Check OpenGL (Mesa software rendering)
    try {
      const glResult = await new Promise((resolve, reject) => {
        execFile("xvfb-run", ["--auto-servernum", "--server-args=-screen 0 1024x768x24", "glxinfo", "-B"], {
          timeout: 15000,
          env: { ...process.env, LIBGL_ALWAYS_SOFTWARE: "1", MESA_DEBUG: "silent" },
        }, (err, stdout, stderr) => {
          if (err) reject(err);
          else resolve({ stdout, stderr });
        });
      });
      // Extract renderer info
      const rendererMatch = glResult.stdout.match(/OpenGL renderer string: (.+)/);
      const versionMatch = glResult.stdout.match(/OpenGL version string: (.+)/);
      checks.opengl = {
        status: "ok",
        renderer: rendererMatch ? rendererMatch[1].trim() : "unknown",
        version: versionMatch ? versionMatch[1].trim() : "unknown",
      };
    } catch (e) {
      checks.opengl = { status: "warn", error: e.message || "glxinfo not available (optional)" };
    }

    // Determine overall status
    const failedChecks = Object.entries(checks).filter(([_, v]) => v.status === "fail");
    const allOk = failedChecks.length === 0;

    res.status(allOk ? 200 : 503).json({
      status: allOk ? "ok" : "fail",
      checks,
      failed: failedChecks.map(([k, _]) => k),
    });
  } catch (e) {
    res.status(503).json({
      status: "error",
      error: e.message,
      checks,
    });
  }
});

// Files check - validates config directories and lists available config files
app.get("/files_check", async (_req, res) => {
  try {
    // Check directories exist
    const dirsExist = {
      machine: fs.existsSync(PRINTER_MACHINE_DIR),
      process: fs.existsSync(PRINT_QUALITY_DIR),
      filament: fs.existsSync(FILAMENT_CONFIG_DIR),
    };

    const missingDirs = Object.entries(dirsExist)
      .filter(([_, exists]) => !exists)
      .map(([name, _]) => name);

    if (missingDirs.length > 0) {
      return res.status(503).json({
        status: "fail",
        error: "Missing config directories",
        missing: missingDirs,
      });
    }

    // List all available configs
    const [printerMachineConfigs, printQualityConfigs, filamentConfigs] = await Promise.all([
      fsp.readdir(PRINTER_MACHINE_DIR).catch(() => []),
      fsp.readdir(PRINT_QUALITY_DIR).catch(() => []),
      fsp.readdir(FILAMENT_CONFIG_DIR).catch(() => []),
    ]);

    const configs = {
      printerMachine: printerMachineConfigs.filter(f => f.endsWith(".json")),
      printQuality: printQualityConfigs.filter(f => f.endsWith(".json")),
      filament: filamentConfigs.filter(f => f.endsWith(".json")),
    };

    res.json({
      status: "ok",
      counts: {
        printerMachine: configs.printerMachine.length,
        printQuality: configs.printQuality.length,
        filament: configs.filament.length,
      },
      configs,
      defaults: {
        printerMachine: DEFAULT_PRINTER_MACHINE,
        printQuality: DEFAULT_PRINT_QUALITY,
        filament: DEFAULT_FILAMENT_CONFIG,
      },
    });
  } catch (e) {
    console.error(e);
    res.status(500).json({ status: "error", error: "Failed to check files" });
  }
});

// ------------ Main API ------------
app.post("/process-order", async (req, res) => {
  const requestId = generateRequestId();
  const requestWorkDir = path.join(WORK_DIR, requestId);

  try {
    const { order } = req.body || {};

    if (!order || !order.orderId) {
      return res.status(400).json({ error: "order with orderId required" });
    }
    if (!Array.isArray(order.items) || order.items.length === 0) {
      return res.status(400).json({ error: "order.items[] required" });
    }
    if (order.items.length > MAX_FILES) {
      return res.status(413).json({ error: `Too many items (max ${MAX_FILES})` });
    }

    // Create request-specific work directory (shared by all items in this order)
    await fsp.mkdir(requestWorkDir, { recursive: true });

    const results = [];
    let totalCost = 0;
    let totalPrintingHours = 0;

    for (let i = 0; i < order.items.length; i++) {
      const item = order.items[i];
      const itemRequestId = `${requestId}-${i}`;

      // Validate required fields
      if (!item.fileName || typeof item.fileName !== "string") {
        await fsp.rm(requestWorkDir, { recursive: true, force: true }).catch(() => {});
        return res.status(400).json({ error: "Each item needs fileName" });
      }
      if (!item.data || typeof item.data !== "string") {
        await fsp.rm(requestWorkDir, { recursive: true, force: true }).catch(() => {});
        return res.status(400).json({ error: `Item ${item.fileName} needs data (base64)` });
      }

      const safe = sanitizeFilename(item.fileName);
      // Use unique filename per item to avoid collisions if same name sent twice
      const uniqueFileName = `${i}_${safe}`;
      const filePath = path.join(requestWorkDir, uniqueFileName);

      const buf = Buffer.from(item.data, "base64");
      if (buf.length > MAX_FILE_BYTES) {
        await fsp.rm(requestWorkDir, { recursive: true, force: true }).catch(() => {});
        return res.status(413).json({ error: `File ${safe} exceeds max size (${MAX_FILE_BYTES} bytes)` });
      }

      await fsp.writeFile(filePath, buf);

      // Validate and resolve config file names (each item can have different configs)
      const printerMachine = item.printerMachine ? sanitizeConfigName(item.printerMachine) : DEFAULT_PRINTER_MACHINE;
      const printQuality = item.printQuality ? sanitizeConfigName(item.printQuality) : DEFAULT_PRINT_QUALITY;
      const filamentConfig = item.filamentConfig ? sanitizeConfigName(item.filamentConfig) : DEFAULT_FILAMENT_CONFIG;

      // Verify configs exist
      if (!(await configFileExists(PRINTER_MACHINE_DIR, printerMachine))) {
        await fsp.rm(requestWorkDir, { recursive: true, force: true }).catch(() => {});
        return res.status(400).json({ error: `Printer machine config not found: ${printerMachine}` });
      }
      if (!(await configFileExists(PRINT_QUALITY_DIR, printQuality))) {
        await fsp.rm(requestWorkDir, { recursive: true, force: true }).catch(() => {});
        return res.status(400).json({ error: `Print quality config not found: ${printQuality}` });
      }
      if (!(await configFileExists(FILAMENT_CONFIG_DIR, filamentConfig))) {
        await fsp.rm(requestWorkDir, { recursive: true, force: true }).catch(() => {});
        return res.status(400).json({ error: `Filament config not found: ${filamentConfig}` });
      }

      // quantity, filamentPrice, color - used in final calculations only, not sent to slicer
      const quantity = Math.max(1, parseInt(item.quantity, 10) || 1);
      const filamentPrice = parseFloat(item.filamentPrice) || 12;
      const color = item.color || "default";

      // Slicing parameters (optional overrides)
      const wallLoops = item.wallLoops ? String(parseInt(item.wallLoops, 10)) : "";
      const infillDensity = item.infillDensity ? String(item.infillDensity) : "";

      // Build environment for Python script
      const env = {
        LIBGL_ALWAYS_SOFTWARE: "1",
        QT_OPENGL: "software",
        QT_QUICK_BACKEND: "software",
        QT_QPA_PLATFORM: "offscreen",
        QTWEBENGINE_DISABLE_SANDBOX: "1",
        QTWEBENGINE_CHROMIUM_FLAGS: "--no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --single-process --no-zygote --in-process-gpu --disable-features=UseOzonePlatform",
        HOME: "/config/.bambu_home",
        XDG_RUNTIME_DIR: "/config/.xdg",
        // Config file names (not full paths - Python builds paths)
        PRINTER_MACHINE_NAME: printerMachine,
        PRINT_QUALITY_NAME: printQuality,
        FILAMENT_CONFIG_NAME: filamentConfig,
        FILAMENT_PRICE: String(filamentPrice),
        // Slicing parameter overrides (empty string = use defaults from config)
        WALL_LOOPS: wallLoops,
        INFILL_DENSITY: infillDensity,
      };

      try {
        const { stdout, stderr } = await runPython(filePath, itemRequestId, env);

        // Parse structured JSON result from Python
        const resultJson = parseResultJson(stdout);

        if (resultJson) {
          // Slicer returns cost for 1 unit - multiply by quantity for totals
          const unitCost = resultJson.total_cost;
          const itemTotalCost = unitCost * quantity;
          const itemTotalHours = resultJson.printing_hours * quantity;

          totalCost += itemTotalCost;
          totalPrintingHours += itemTotalHours;

          results.push({
            fileName: safe,
            quantity,
            unitCost: Math.round(unitCost * 100) / 100,
            totalCost: Math.round(itemTotalCost * 100) / 100,
            printingHours: resultJson.printing_hours,
            totalPrintingHours: Math.round(itemTotalHours * 100) / 100,
            filamentGrams: resultJson.filament_grams,
            filamentCost: resultJson.filament_cost,
            depreciationCost: resultJson.depreciation_cost,
            electricityCost: resultJson.electricity_cost,
            color,
            config: {
              printerMachine,
              printQuality,
              filament: filamentConfig,
            },
          });
        } else {
          // Fallback to legacy parsing
          const costMatch = stdout.match(/Total Cost:\s*([\d.]+)/);
          const unitCost = costMatch ? parseFloat(costMatch[1]) : 0;
          const itemTotalCost = unitCost * quantity;
          totalCost += itemTotalCost;

          results.push({
            fileName: safe,
            quantity,
            unitCost: Math.round(unitCost * 100) / 100,
            totalCost: Math.round(itemTotalCost * 100) / 100,
            color,
            config: {
              printerMachine,
              printQuality,
              filament: filamentConfig,
            },
            rawOutput: stdout,
          });
        }
      } catch (err) {
        console.error("Slicer error:", err.stderr || err.stdout || err);
        await fsp.rm(requestWorkDir, { recursive: true, force: true }).catch(() => {});
        return res.status(500).json({
          error: `Slicing failed for ${safe}`,
          details: err.stderr || err.message,
        });
      }
    }

    // Cleanup request work directory (all STL files for this order)
    try {
      await fsp.rm(requestWorkDir, { recursive: true, force: true });
    } catch (e) {
      console.error("Cleanup error:", e);
    }

    res.json({
      ok: true,
      orderId: order.orderId,
      requestId,
      itemCount: results.length,
      totalCost: Math.round(totalCost * 100) / 100,
      totalPrintingHours: Math.round(totalPrintingHours * 100) / 100,
      items: results,
    });
  } catch (e) {
    console.error(e);
    // Cleanup on error
    try {
      await fsp.rm(requestWorkDir, { recursive: true, force: true });
    } catch {}
    res.status(500).json({ error: "Internal error" });
  }
});

// ------------ Local Test Endpoints ------------
// For testing: curl http://localhost:8080/local_stl_test
// For testing: curl http://localhost:8080/local_3mf_test
const TEST_STL_PATH = "/app/test_files/tumor.stl";
const TEST_3MF_PATH = "/app/test_files/Great_Wave_bambu.3mf";

app.get("/local_stl_test", async (_req, res) => {
  const requestId = generateRequestId();
  const requestWorkDir = path.join(WORK_DIR, requestId);

  try {
    // Check if test file exists
    if (!fs.existsSync(TEST_STL_PATH)) {
      return res.status(404).json({
        error: "Test file not found",
        path: TEST_STL_PATH,
        hint: "Make sure tumor.stl is copied to /app/test_files/"
      });
    }

    // Create request-specific work directory
    await fsp.mkdir(requestWorkDir, { recursive: true });

    // Copy test STL to work directory
    const workStlPath = path.join(requestWorkDir, "tumor.stl");
    await fsp.copyFile(TEST_STL_PATH, workStlPath);

    console.log(`[local_test] Starting test with requestId=${requestId}`);

    // Build environment for Python script
    const env = {
      LIBGL_ALWAYS_SOFTWARE: "1",
      QT_OPENGL: "software",
      QT_QUICK_BACKEND: "software",
      QT_QPA_PLATFORM: "offscreen",
      QTWEBENGINE_DISABLE_SANDBOX: "1",
      QTWEBENGINE_CHROMIUM_FLAGS: "--no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --single-process --no-zygote --in-process-gpu --disable-features=UseOzonePlatform",
      HOME: "/config/.bambu_home",
      XDG_RUNTIME_DIR: "/config/.xdg",
      PRINTER_MACHINE_NAME: DEFAULT_PRINTER_MACHINE,
      PRINT_QUALITY_NAME: DEFAULT_PRINT_QUALITY,
      FILAMENT_CONFIG_NAME: DEFAULT_FILAMENT_CONFIG,
      FILAMENT_PRICE: "12",
      // Test slicing parameters
      WALL_LOOPS: "4",
      INFILL_DENSITY: "20%",
    };

    const { stdout, stderr } = await runPython(workStlPath, requestId, env);

    // Parse structured JSON result from Python
    const resultJson = parseResultJson(stdout);

    // Cleanup
    try {
      await fsp.rm(requestWorkDir, { recursive: true, force: true });
    } catch (e) {
      console.error("Cleanup error:", e);
    }

    res.json({
      ok: true,
      test: "local_stl_test with tumor.stl",
      requestId,
      result: resultJson,
    });
  } catch (e) {
    console.error("local_stl_test error:", e);
    // Cleanup on error
    try {
      await fsp.rm(requestWorkDir, { recursive: true, force: true });
    } catch {}
    res.status(500).json({
      error: "Test failed",
      message: e.message,
      stderr: e.stderr || null,
      stdout: e.stdout || null,
    });
  }
});

// Local 3MF test - uses pre-sliced 3mf file (Great_Wave_bambu.3mf)
app.get("/local_3mf_test", async (_req, res) => {
  const requestId = generateRequestId();
  const requestWorkDir = path.join(WORK_DIR, requestId);

  try {
    // Check if test file exists
    if (!fs.existsSync(TEST_3MF_PATH)) {
      return res.status(404).json({
        error: "Test file not found",
        path: TEST_3MF_PATH,
        hint: "Make sure Great_Wave_bambu.3mf is copied to /app/test_files/"
      });
    }

    // Create request-specific work directory
    await fsp.mkdir(requestWorkDir, { recursive: true });

    // Copy test 3MF to work directory
    const work3mfPath = path.join(requestWorkDir, "Great_Wave_bambu.3mf");
    await fsp.copyFile(TEST_3MF_PATH, work3mfPath);

    console.log(`[local_3mf_test] Starting test with requestId=${requestId}`);

    // Build environment for Python script
    const env = {
      LIBGL_ALWAYS_SOFTWARE: "1",
      QT_OPENGL: "software",
      QT_QUICK_BACKEND: "software",
      QT_QPA_PLATFORM: "offscreen",
      QTWEBENGINE_DISABLE_SANDBOX: "1",
      QTWEBENGINE_CHROMIUM_FLAGS: "--no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --single-process --no-zygote --in-process-gpu --disable-features=UseOzonePlatform",
      HOME: "/config/.bambu_home",
      XDG_RUNTIME_DIR: "/config/.xdg",
      PRINTER_MACHINE_NAME: DEFAULT_PRINTER_MACHINE,
      PRINT_QUALITY_NAME: DEFAULT_PRINT_QUALITY,
      FILAMENT_CONFIG_NAME: DEFAULT_FILAMENT_CONFIG,
      FILAMENT_PRICE: "12",
      // Test slicing parameters
      WALL_LOOPS: "4",
      INFILL_DENSITY: "20%",
    };

    const { stdout, stderr } = await runPython(work3mfPath, requestId, env);

    // Parse structured JSON result from Python
    const resultJson = parseResultJson(stdout);

    // Cleanup
    try {
      await fsp.rm(requestWorkDir, { recursive: true, force: true });
    } catch (e) {
      console.error("Cleanup error:", e);
    }

    res.json({
      ok: true,
      test: "local_3mf_test with Great_Wave_bambu.3mf",
      requestId,
      result: resultJson,
    });
  } catch (e) {
    console.error("local_3mf_test error:", e);
    // Cleanup on error
    try {
      await fsp.rm(requestWorkDir, { recursive: true, force: true });
    } catch {}
    res.status(500).json({
      error: "Test failed",
      message: e.message,
      stderr: e.stderr || null,
      stdout: e.stdout || null,
    });
  }
});

// Root index
app.get("/", (_req, res) => res.json({
  service: "bambu-slicer-api",
  version: "2.0.0",
  endpoints: ["/process-order", "/healthz", "/system_check", "/files_check", "/local_stl_test", "/local_3mf_test"],
  time: new Date().toISOString()
}));

app.listen(PORT, () => {
  console.log(`Bambu Slicer API running on port ${PORT}`);
});

// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::{
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, RunEvent, WindowEvent,
};

struct Backend(Mutex<Option<Child>>);

/// Определяет рабочую директорию проекта ZoomHub
fn find_project_dir() -> std::path::PathBuf {
    // Ищем рядом с бинарником (для dev и для bundle)
    let exe = std::env::current_exe().unwrap_or_default();
    let exe_dir = exe.parent().unwrap_or(std::path::Path::new("."));

    // В dev mode: бинарник в target/debug, проект — два уровня выше src-tauri
    let candidates = [
        exe_dir.join("../../../"),           // target/debug/ → project root
        exe_dir.join("../../"),
        std::path::PathBuf::from("."),
        std::env::current_dir().unwrap_or_default(),
    ];

    for dir in &candidates {
        let check = dir.join("app/main.py");
        if check.exists() {
            return dir.canonicalize().unwrap_or(dir.clone());
        }
    }

    std::env::current_dir().unwrap_or_default()
}

/// Ищет Python с venv внутри проекта
fn find_python(project_dir: &std::path::Path) -> String {
    // Приоритет: venv проекта
    let venv_python = project_dir.join("venv/bin/python");
    if venv_python.exists() {
        return venv_python.to_string_lossy().to_string();
    }

    let venv_python3 = project_dir.join("venv/bin/python3");
    if venv_python3.exists() {
        return venv_python3.to_string_lossy().to_string();
    }

    // Homebrew / системный
    let fallbacks = [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "python3",
    ];

    for candidate in fallbacks {
        if Command::new(candidate).arg("--version").output().is_ok() {
            return candidate.to_string();
        }
    }

    "python3".to_string()
}

/// Запускает FastAPI-сервер
fn start_backend() -> Option<Child> {
    let project_dir = find_project_dir();
    let python = find_python(&project_dir);
    println!("[ZoomHub] Проект: {}", project_dir.display());
    println!("[ZoomHub] Python: {}", python);
    println!("[ZoomHub] Запускаю бэкенд...");

    match Command::new(&python)
        .args(["-m", "uvicorn", "app.main:app", "--port", "8002", "--host", "127.0.0.1"])
        .current_dir(&project_dir)
        .spawn()
    {
        Ok(child) => {
            println!("[ZoomHub] Бэкенд запущен (PID: {})", child.id());
            Some(child)
        }
        Err(e) => {
            eprintln!("[ZoomHub] Не удалось запустить бэкенд: {}", e);
            None
        }
    }
}

/// Останавливает FastAPI-сервер
fn stop_backend(child: &mut Child) {
    println!("[ZoomHub] Останавливаю бэкенд (PID: {})...", child.id());

    // Сначала SIGTERM (graceful shutdown)
    #[cfg(unix)]
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }

    // Ждём 3 секунды, потом kill
    std::thread::sleep(std::time::Duration::from_secs(3));
    let _ = child.kill();
    let _ = child.wait();
    println!("[ZoomHub] Бэкенд остановлен");
}

/// Проверяет, запущен ли бэкенд
fn is_backend_running() -> bool {
    std::process::Command::new("curl")
        .args(["-s", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8002/health"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains("200"))
        .unwrap_or(false)
}

fn main() {
    // Запускаем бэкенд только если он не запущен (в dev mode его запускает beforeDevCommand)
    let backend_child = if is_backend_running() {
        println!("[ZoomHub] Бэкенд уже запущен");
        None
    } else {
        start_backend()
    };

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Backend(Mutex::new(backend_child)))
        .setup(|app| {
            // System tray
            let _tray = TrayIconBuilder::new()
                .tooltip("ZoomHub — AI Companion для встреч")
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            if window.is_visible().unwrap_or(false) {
                                let _ = window.hide();
                            } else {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Ошибка инициализации Tauri");

    app.run(|app_handle, event| {
        match event {
            // При закрытии окна — прячем в tray вместо выхода
            RunEvent::WindowEvent {
                event: WindowEvent::CloseRequested { api, .. },
                ..
            } => {
                api.prevent_close();
                if let Some(window) = app_handle.get_webview_window("main") {
                    let _ = window.hide();
                }
            }
            // При выходе из приложения — останавливаем бэкенд
            RunEvent::ExitRequested { .. } => {
                let state = app_handle.state::<Backend>();
                let mut guard = state.0.lock().unwrap();
                if let Some(ref mut child) = *guard {
                    stop_backend(child);
                }
            }
            _ => {}
        }
    });
}

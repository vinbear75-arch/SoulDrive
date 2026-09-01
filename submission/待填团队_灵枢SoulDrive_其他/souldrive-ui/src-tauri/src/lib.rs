use std::{
    net::TcpListener,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::Mutex,
};
use tauri::{Emitter, Manager, Runtime};

#[cfg(windows)]
use std::{
    ffi::OsStr,
    os::windows::{ffi::OsStrExt, process::CommandExt},
};
#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE},
    System::Threading::CreateMutexW,
    UI::{
        Input::KeyboardAndMouse::{
            RegisterHotKey, UnregisterHotKey, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT,
        },
        WindowsAndMessaging::{GetMessageW, MSG, WM_HOTKEY},
    },
};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

struct BackendProcess {
    child: Mutex<Option<Child>>,
    api_token: String,
    api_port: u16,
}

impl BackendProcess {
    fn shutdown(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                graceful_shutdown(&self.api_token, self.api_port);
                terminate_process_tree(&mut child);
            }
        }
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[cfg(windows)]
struct InstanceGuard(usize);

#[cfg(windows)]
unsafe impl Send for InstanceGuard {}

#[cfg(windows)]
unsafe impl Sync for InstanceGuard {}

#[cfg(windows)]
impl Drop for InstanceGuard {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0 as HANDLE) };
    }
}

#[cfg(windows)]
fn acquire_single_instance() -> std::io::Result<InstanceGuard> {
    let name: Vec<u16> = OsStr::new("Local\\SoulDriveDesktopSingleInstance")
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let handle = unsafe { CreateMutexW(std::ptr::null(), 1, name.as_ptr()) };
    if handle.is_null() {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
        unsafe { CloseHandle(handle) };
        return Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "SoulDrive is already running",
        ));
    }
    Ok(InstanceGuard(handle as usize))
}

#[cfg(not(windows))]
struct InstanceGuard;

#[cfg(not(windows))]
fn acquire_single_instance() -> std::io::Result<InstanceGuard> {
    Ok(InstanceGuard)
}

fn graceful_shutdown(api_token: &str, api_port: u16) {
    let url = format!("http://127.0.0.1:{}/shutdown", api_port);
    let client = reqwest::blocking::Client::new();
    let _ = client
        .post(&url)
        .header("X-SoulDrive-Token", api_token)
        .timeout(std::time::Duration::from_secs(5))
        .send();
    std::thread::sleep(std::time::Duration::from_millis(500));
}

#[cfg(windows)]
fn terminate_process_tree(child: &mut Child) {
    let _ = Command::new("taskkill")
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .status();
    let _ = child.wait();
}

#[cfg(not(windows))]
fn terminate_process_tree(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

#[tauri::command]
fn backend_status(state: tauri::State<'_, BackendProcess>) -> bool {
    let Ok(mut guard) = state.child.lock() else {
        return false;
    };

    let Some(child) = guard.as_mut() else {
        return false;
    };

    match child.try_wait() {
        Ok(Some(_)) => {
            *guard = None;
            false
        }
        Ok(None) => true,
        Err(_) => false,
    }
}

#[derive(serde::Serialize)]
struct RuntimeConfig {
    base_url: String,
    api_token: String,
}

#[derive(serde::Serialize)]
struct SkillApiResponse {
    status: u16,
    body: String,
}

#[derive(Clone, serde::Serialize)]
struct UiAutomationRect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}

#[derive(Clone, serde::Serialize)]
struct UiAutomationElement {
    target_id: String,
    target_kind: String,
    control_type_id: i32,
    name: Option<String>,
    selected_text: Option<String>,
    automation_id: Option<String>,
    class_name: Option<String>,
    framework_id: Option<String>,
    process_id: i32,
    native_window_handle: Option<u64>,
    is_focused: bool,
    is_password: bool,
    is_enabled: bool,
    is_offscreen: bool,
    screen_bbox: Option<UiAutomationRect>,
}

#[derive(Clone, serde::Serialize)]
struct UiAutomationSnapshot {
    schema_version: String,
    status: String,
    source: String,
    captured_at_ms: u64,
    element: Option<UiAutomationElement>,
    reason: Option<String>,
}

fn unix_timestamp_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or(0)
}

fn unavailable_ui_automation(reason: &str) -> UiAutomationSnapshot {
    UiAutomationSnapshot {
        schema_version: "souldrive_uia_snapshot_v0.1".to_string(),
        status: "unavailable".to_string(),
        source: "windows_uia".to_string(),
        captured_at_ms: unix_timestamp_ms(),
        element: None,
        reason: Some(reason.to_string()),
    }
}

#[cfg(windows)]
fn safe_ui_automation_text_bounded(value: &str, redact: bool, max_chars: usize) -> Option<String> {
    if redact {
        return None;
    }
    let normalized: String = value
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .collect();
    let compact = normalized.split_whitespace().collect::<Vec<_>>().join(" ");
    let bounded: String = compact.chars().take(max_chars).collect();
    (!bounded.is_empty()).then_some(bounded)
}

#[cfg(windows)]
fn safe_ui_automation_text(value: &str, redact: bool) -> Option<String> {
    safe_ui_automation_text_bounded(value, redact, 260)
}

#[cfg(windows)]
fn ui_automation_control_type(control_type_id: i32) -> String {
    match control_type_id {
        50000 => "button",
        50001 => "calendar",
        50002 => "checkbox",
        50003 => "combobox",
        50004 => "edit",
        50005 => "hyperlink",
        50006 => "image",
        50007 => "list_item",
        50008 => "list",
        50009 => "menu",
        50010 => "menu_bar",
        50011 => "menu_item",
        50012 => "progress_bar",
        50013 => "radio_button",
        50014 => "scroll_bar",
        50015 => "slider",
        50016 => "spinner",
        50017 => "status_bar",
        50018 => "tab",
        50019 => "tab_item",
        50020 => "text",
        50021 => "tool_bar",
        50022 => "tool_tip",
        50023 => "tree",
        50024 => "tree_item",
        50025 => "custom",
        50026 => "group",
        50027 => "thumb",
        50028 => "data_grid",
        50029 => "data_item",
        50030 => "document",
        50031 => "split_button",
        50032 => "window",
        50033 => "pane",
        50034 => "header",
        50035 => "header_item",
        50036 => "table",
        50037 => "title_bar",
        50038 => "separator",
        50039 => "semantic_zoom",
        50040 => "app_bar",
        _ => "unknown",
    }
    .to_string()
}

#[cfg(windows)]
fn ui_automation_target_id(
    process_id: i32,
    native_window_handle: Option<u64>,
    control_type_id: i32,
    automation_id: Option<&str>,
    name: Option<&str>,
) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for part in [
        process_id.to_string(),
        native_window_handle.unwrap_or_default().to_string(),
        control_type_id.to_string(),
        automation_id.unwrap_or_default().to_string(),
        name.unwrap_or_default().to_string(),
    ] {
        for byte in part.as_bytes() {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x100000001b3);
        }
        hash ^= 0xff;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("uia-{hash:016x}")
}

#[cfg(windows)]
fn bstr_text_bounded(
    value: Option<windows::core::BSTR>,
    redact: bool,
    max_chars: usize,
) -> Option<String> {
    let value = value?;
    let text = String::try_from(&value).ok()?;
    safe_ui_automation_text_bounded(&text, redact, max_chars)
}

#[cfg(windows)]
fn bstr_text(value: Option<windows::core::BSTR>, redact: bool) -> Option<String> {
    let value = value?;
    let text = String::try_from(&value).ok()?;
    safe_ui_automation_text(&text, redact)
}

#[cfg(windows)]
fn selected_text_from_ui_automation(
    element: &windows::Win32::UI::Accessibility::IUIAutomationElement,
    is_password: bool,
) -> Option<String> {
    use windows::Win32::UI::Accessibility::{IUIAutomationTextPattern, UIA_TextPatternId};

    if is_password {
        return None;
    }
    let pattern: IUIAutomationTextPattern =
        unsafe { element.GetCurrentPatternAs(UIA_TextPatternId) }.ok()?;
    let ranges = unsafe { pattern.GetSelection() }.ok()?;
    let range_count = unsafe { ranges.Length() }.ok()?.clamp(0, 4);
    let mut fragments = Vec::new();
    for index in 0..range_count {
        let Ok(range) = (unsafe { ranges.GetElement(index) }) else {
            continue;
        };
        let Ok(text) = (unsafe { range.GetText(4000) }) else {
            continue;
        };
        if let Some(fragment) = bstr_text_bounded(Some(text), false, 4000) {
            fragments.push(fragment);
        }
    }
    if fragments.is_empty() {
        return None;
    }
    safe_ui_automation_text_bounded(&fragments.join(" "), false, 4000)
}

#[cfg(windows)]
fn capture_focused_ui_element_windows() -> UiAutomationSnapshot {
    use windows::Win32::{
        Foundation::HWND,
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER,
            COINIT_APARTMENTTHREADED,
        },
        UI::Accessibility::{CUIAutomation8, IUIAutomation},
    };

    let initialized = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) };
    if !initialized.is_ok() {
        return unavailable_ui_automation("com_initialize_failed");
    }

    let snapshot: Result<UiAutomationSnapshot, &'static str> = (|| {
        let automation: IUIAutomation =
            unsafe { CoCreateInstance(&CUIAutomation8, None, CLSCTX_INPROC_SERVER) }
                .map_err(|_| "uia_provider_unavailable")?;
        let element =
            unsafe { automation.GetFocusedElement() }.map_err(|_| "focused_element_unavailable")?;

        let password_state = unsafe { element.CurrentIsPassword() };
        let password_state_unknown = password_state.is_err();
        let is_password = password_state.map(|value| value.as_bool()).unwrap_or(true);
        let process_id = unsafe { element.CurrentProcessId() }
            .unwrap_or_default()
            .max(0);
        let control_type_id = unsafe { element.CurrentControlType() }
            .map(|value| value.0)
            .unwrap_or_default();
        let native_window_handle = unsafe { element.CurrentNativeWindowHandle() }
            .ok()
            .map(|handle: HWND| handle.0 as usize as u64)
            .filter(|handle| *handle != 0);
        let name = bstr_text(unsafe { element.CurrentName() }.ok(), is_password);
        let selected_text = selected_text_from_ui_automation(&element, is_password);
        let automation_id = bstr_text(unsafe { element.CurrentAutomationId() }.ok(), is_password);
        let class_name = bstr_text(unsafe { element.CurrentClassName() }.ok(), is_password);
        let framework_id = bstr_text(unsafe { element.CurrentFrameworkId() }.ok(), is_password);
        let screen_bbox = unsafe { element.CurrentBoundingRectangle() }
            .ok()
            .filter(|rect| rect.right > rect.left && rect.bottom > rect.top)
            .map(|rect| UiAutomationRect {
                left: rect.left,
                top: rect.top,
                right: rect.right,
                bottom: rect.bottom,
            });
        let target_id = ui_automation_target_id(
            process_id,
            native_window_handle,
            control_type_id,
            automation_id.as_deref(),
            name.as_deref(),
        );

        Ok(UiAutomationSnapshot {
            schema_version: "souldrive_uia_snapshot_v0.1".to_string(),
            status: if is_password { "redacted" } else { "pass" }.to_string(),
            source: "windows_uia".to_string(),
            captured_at_ms: unix_timestamp_ms(),
            element: Some(UiAutomationElement {
                target_id,
                target_kind: ui_automation_control_type(control_type_id),
                control_type_id,
                name,
                selected_text,
                automation_id,
                class_name,
                framework_id,
                process_id,
                native_window_handle,
                is_focused: unsafe { element.CurrentHasKeyboardFocus() }
                    .map(|value| value.as_bool())
                    .unwrap_or(true),
                is_password,
                is_enabled: unsafe { element.CurrentIsEnabled() }
                    .map(|value| value.as_bool())
                    .unwrap_or(false),
                is_offscreen: unsafe { element.CurrentIsOffscreen() }
                    .map(|value| value.as_bool())
                    .unwrap_or(false),
                screen_bbox,
            }),
            reason: if password_state_unknown {
                Some("password_state_unavailable_redacted".to_string())
            } else if is_password {
                Some("password_control_redacted".to_string())
            } else {
                None
            },
        })
    })();

    unsafe { CoUninitialize() };
    snapshot.unwrap_or_else(|_| unavailable_ui_automation("uia_property_read_failed"))
}

#[tauri::command]
fn capture_focused_ui_element() -> UiAutomationSnapshot {
    #[cfg(windows)]
    {
        return std::thread::Builder::new()
            .name("souldrive-uia-capture".to_string())
            .spawn(capture_focused_ui_element_windows)
            .ok()
            .and_then(|thread| thread.join().ok())
            .unwrap_or_else(|| unavailable_ui_automation("uia_thread_failed"));
    }

    #[cfg(not(windows))]
    unavailable_ui_automation("windows_only")
}

#[cfg(windows)]
const UIA_HOTKEY_ID: i32 = 0x5344;

#[cfg(windows)]
fn start_ui_automation_hotkey<R: Runtime>(app: tauri::AppHandle<R>) {
    let _ = std::thread::Builder::new()
        .name("souldrive-uia-hotkey".to_string())
        .spawn(move || {
            let registered = unsafe {
                RegisterHotKey(
                    std::ptr::null_mut(),
                    UIA_HOTKEY_ID,
                    MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT,
                    b'U' as u32,
                )
            };
            if registered == 0 {
                eprintln!("SoulDrive UI Automation hotkey registration failed");
                return;
            }

            let mut message: MSG = unsafe { std::mem::zeroed() };
            loop {
                let result = unsafe { GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) };
                if result <= 0 {
                    break;
                }
                if message.message == WM_HOTKEY && message.wParam == UIA_HOTKEY_ID as usize {
                    let snapshot = capture_focused_ui_element();
                    let _ = app.emit("souldrive-uia-capture", snapshot);
                }
            }

            unsafe { UnregisterHotKey(std::ptr::null_mut(), UIA_HOTKEY_ID) };
        });
}

#[cfg(windows)]
#[cfg(test)]
mod ui_automation_tests {
    use super::safe_ui_automation_text;

    #[test]
    fn ui_automation_text_is_bounded_and_password_safe() {
        assert_eq!(
            safe_ui_automation_text("  Button\n\tName  ", false),
            Some("Button Name".to_string())
        );
        assert_eq!(safe_ui_automation_text("secret", true), None);
        assert_eq!(
            safe_ui_automation_text(&"x".repeat(300), false)
                .unwrap()
                .len(),
            260
        );
    }

    #[test]
    fn unavailable_ui_automation_is_explicit_and_empty() {
        let snapshot = super::unavailable_ui_automation("focused_element_unavailable");
        assert_eq!(snapshot.status, "unavailable");
        assert_eq!(snapshot.source, "windows_uia");
        assert!(snapshot.element.is_none());
        assert_eq!(
            snapshot.reason.as_deref(),
            Some("focused_element_unavailable")
        );
    }

    #[test]
    fn skill_api_proxy_only_allows_expected_local_routes() {
        assert!(super::allowed_skill_api_request("GET", "/learning/skills"));
        assert!(super::allowed_skill_api_request(
            "DELETE",
            "/learning/skills/skill_123"
        ));
        assert!(super::allowed_skill_api_request(
            "POST",
            "/learning/skills/inbox/frontend-slides/approve"
        ));
        assert!(super::allowed_skill_api_request(
            "POST",
            "/learning/skills/inbox/frontend-slides/activate"
        ));
        assert!(super::allowed_skill_api_request(
            "POST",
            "/learning/skills/skill_123/presentation-style"
        ));
        assert!(super::allowed_skill_api_request(
            "POST",
            "/learning/skills/skill_123/runs"
        ));
        assert!(!super::allowed_skill_api_request("GET", "/documents/list"));
        assert!(!super::allowed_skill_api_request(
            "DELETE",
            "/learning/skills/../documents"
        ));
    }
}

#[tauri::command]
fn runtime_config(state: tauri::State<'_, BackendProcess>) -> RuntimeConfig {
    RuntimeConfig {
        base_url: format!("http://127.0.0.1:{}", state.api_port),
        api_token: state.api_token.clone(),
    }
}

fn allowed_skill_api_request(method: &str, path: &str) -> bool {
    let path_chars_are_safe = path
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '/' | '-' | '_' | '.' | '%'));
    if !path_chars_are_safe {
        return false;
    }
    matches!(
        (method, path),
        ("GET", "/learning/skills")
            | ("GET", "/learning/skills/inbox")
            | ("POST", "/learning/skills/sync")
            | ("POST", "/learning/skills/install")
    ) || (method == "POST"
          && path.starts_with("/learning/skills/inbox/")
          && (path.ends_with("/approve") || path.ends_with("/activate")))
        || (method == "POST"
            && path.starts_with("/learning/skills/")
            && path.ends_with("/presentation-style")
            && path["/learning/skills/".len()..]
                .matches('/')
                .count()
                == 1)
        || (method == "POST"
            && path.starts_with("/learning/skills/")
            && path.ends_with("/runs")
            && path["/learning/skills/".len()..]
                .matches('/')
                .count()
                == 1)
        || (method == "DELETE"
            && path.starts_with("/learning/skills/")
            && !path["/learning/skills/".len()..].contains('/'))
}

#[tauri::command]
async fn skill_api_request(
    state: tauri::State<'_, BackendProcess>,
    method: String,
    path: String,
    body: Option<String>,
) -> Result<SkillApiResponse, String> {
    let method = method.to_ascii_uppercase();
    if !allowed_skill_api_request(&method, &path) {
        return Err("unsupported Skill API request".to_string());
    }
    let api_port = state.api_port;
    let api_token = state.api_token.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let client = reqwest::blocking::Client::new();
        let request_method = reqwest::Method::from_bytes(method.as_bytes())
            .map_err(|error| error.to_string())?;
        let mut request = client
            .request(
                request_method,
                format!("http://127.0.0.1:{api_port}{path}"),
            )
            .header("X-SoulDrive-Token", api_token)
            .timeout(std::time::Duration::from_secs(120));
        if let Some(body) = body {
            request = request.header("Content-Type", "application/json").body(body);
        }
        let response = request.send().map_err(|error| error.to_string())?;
        let status = response.status().as_u16();
        let body = response.text().map_err(|error| error.to_string())?;
        Ok(SkillApiResponse { status, body })
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
fn select_document_files() -> Vec<String> {
    rfd::FileDialog::new()
        .add_filter("PDF 与图像", &["pdf", "png", "jpg", "jpeg", "webp"])
        .pick_files()
        .unwrap_or_default()
        .into_iter()
        .map(|path| path.to_string_lossy().to_string())
        .collect()
}

#[tauri::command]
fn select_project_folder() -> Option<String> {
    rfd::FileDialog::new()
        .pick_folder()
        .map(|path| path.to_string_lossy().to_string())
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    let path = PathBuf::from(path);
    if !path.exists() {
        return Err("path not found".to_string());
    }

    #[cfg(windows)]
    {
        let mut command = Command::new("explorer");
        if path.is_file() {
            command.arg("/select,").arg(path);
        } else {
            command.arg(path);
        }
        command.spawn().map_err(|error| error.to_string())?;
        return Ok(());
    }

    #[cfg(not(windows))]
    {
        let target = if path.is_file() { path.parent().map(PathBuf::from).unwrap_or(path) } else { path };
        Command::new("xdg-open").arg(target).spawn().map_err(|error| error.to_string())?;
        Ok(())
    }
}

#[tauri::command]
fn open_in_editor(path: String) -> Result<String, String> {
    // This command only opens an explicitly selected local Skill file. It
    // never executes the file or any command contained in it.
    let path = PathBuf::from(path);
    if !path.is_file() {
        return Err("editor target file not found".to_string());
    }

    #[cfg(windows)]
    {
        if Command::new("code.exe").arg(&path).spawn().is_ok()
            || Command::new("code.cmd").arg(&path).spawn().is_ok()
        {
            return Ok("vscode".to_string());
        }
        Command::new("notepad.exe")
            .arg(&path)
            .spawn()
            .map_err(|error| error.to_string())?;
        return Ok("notepad".to_string());
    }

    #[cfg(not(windows))]
    {
        if Command::new("code").arg(&path).spawn().is_ok() {
            return Ok("vscode".to_string());
        }
        Command::new("xdg-open")
            .arg(&path)
            .spawn()
            .map_err(|error| error.to_string())?;
        Ok("default".to_string())
    }
}

fn spawn_backend<R: Runtime>(
    app: &impl Manager<R>,
    api_token: &str,
    api_port: u16,
) -> std::io::Result<Child> {
    let runtime_root = runtime_root(app);
    let default_workspace_root = app
        .path()
        .app_data_dir()
        .map_err(|error| std::io::Error::other(error.to_string()))?
        .join("workspace");
    let workspace_root = std::env::var_os("SOULDRIVE_WORKSPACE")
        .map(PathBuf::from)
        .filter(|path| path.is_dir() || !path.exists())
        .or_else(|| {
            cfg!(debug_assertions)
                .then(|| development_project_root().join("runtime").join("workspace"))
        })
        .unwrap_or(default_workspace_root);
    let model_root = std::env::var_os("SOULDRIVE_MODEL_DIR")
        .map(PathBuf::from)
        .filter(|path| path.is_dir())
        .unwrap_or_else(|| runtime_root.join("models"));
    let workspace_mode =
        std::env::var("SOULDRIVE_WORKSPACE_MODE").unwrap_or_else(|_| "local".to_string());
    let watch_removable = if workspace_mode == "removable" {
        "1"
    } else {
        "0"
    };
    let configured_sidecar = if prefer_source_backend() {
        None
    } else {
        sidecar_executable(app, &runtime_root)
    };

    if let Some(sidecar_path) = configured_sidecar {
        let mut command = Command::new(sidecar_path);
        command
            .env("SOULDRIVE_API_TOKEN", api_token)
            .env("SOULDRIVE_API_PORT", api_port.to_string())
            .env("SOULDRIVE_APP_ROOT", &runtime_root)
            .env("SOULDRIVE_MODEL_DIR", &model_root)
            .env("SOULDRIVE_WORKSPACE", &workspace_root)
            .env("SOULDRIVE_WORKSPACE_MODE", &workspace_mode)
            .env("SOULDRIVE_PC_ONLY", "1")
            .env("SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK", "1")
            .env("SOULDRIVE_DEV_WORKSPACE_PASSPHRASE", "88888888")
            .env("SOULDRIVE_WATCH_REMOVABLE", watch_removable)
            .env("SOULDRIVE_CHAT_PROVIDER", "ollama")
            .env("SOULDRIVE_OLLAMA_MODEL", "qwen3.5:9b")
            .env("SOULDRIVE_EMBEDDING_PROVIDER", "ollama")
            .env("SOULDRIVE_OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
            .env("SOULDRIVE_REDIS_ENABLED", "1")
            .env("SOULDRIVE_REDIS_URL", "redis://127.0.0.1:6379/0")
            .env("SOULDRIVE_REDIS_MAX_MESSAGES", "32")
            .env("SOULDRIVE_REDIS_TTL_SECONDS", "604800")
            .env("SOULDRIVE_POSTGRES_ENABLED", "1")
            .env(
                "SOULDRIVE_POSTGRES_URL",
                "postgresql://souldrive:souldrive@127.0.0.1:5432/souldrive",
            )
            .env("SOULDRIVE_SESSION_COMPACTION_THRESHOLD", "16")
            .env("SOULDRIVE_MAX_BODY_BYTES", "16777216")
            .current_dir(&runtime_root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());

        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);

        command
            .env("SOULDRIVE_PARENT_PID", std::process::id().to_string())
            .env("SOULDRIVE_PARENT_EXIT_GRACE_SECONDS", "30");
        return command.spawn();
    }

    let mut command = source_backend_command();
    command
        .env("SOULDRIVE_API_TOKEN", api_token)
        .env("SOULDRIVE_API_PORT", api_port.to_string())
        .env("SOULDRIVE_APP_ROOT", &runtime_root)
        .env("SOULDRIVE_MODEL_DIR", &model_root)
        .env("SOULDRIVE_WORKSPACE", &workspace_root)
        .env("SOULDRIVE_WORKSPACE_MODE", &workspace_mode)
        .env("SOULDRIVE_PC_ONLY", "1")
        .env("SOULDRIVE_DEV_BYPASS_WORKSPACE_LOCK", "1")
        .env("SOULDRIVE_DEV_WORKSPACE_PASSPHRASE", "88888888")
        .env("SOULDRIVE_WATCH_REMOVABLE", watch_removable)
        .env("SOULDRIVE_CHAT_PROVIDER", "ollama")
        .env("SOULDRIVE_OLLAMA_MODEL", "qwen3.5:9b")
        .env("SOULDRIVE_EMBEDDING_PROVIDER", "ollama")
        .env("SOULDRIVE_OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
        .env("SOULDRIVE_REDIS_ENABLED", "1")
        .env("SOULDRIVE_REDIS_URL", "redis://127.0.0.1:6379/0")
        .env("SOULDRIVE_REDIS_MAX_MESSAGES", "32")
        .env("SOULDRIVE_REDIS_TTL_SECONDS", "604800")
        .env("SOULDRIVE_POSTGRES_ENABLED", "1")
        .env(
            "SOULDRIVE_POSTGRES_URL",
            "postgresql://souldrive:souldrive@127.0.0.1:5432/souldrive",
        )
        .env("SOULDRIVE_SESSION_COMPACTION_THRESHOLD", "16")
        .env("SOULDRIVE_MAX_BODY_BYTES", "16777216")
        .current_dir(&runtime_root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);

    command
        .env("SOULDRIVE_PARENT_PID", std::process::id().to_string())
        .env("SOULDRIVE_PARENT_EXIT_GRACE_SECONDS", "30");
    command.spawn()
}

fn prefer_source_backend() -> bool {
    cfg!(debug_assertions)
        && std::env::var("SOULDRIVE_DEV_USE_PACKAGED_SIDECAR")
            .map(|value| value != "1")
            .unwrap_or(true)
        && std::env::var("SOULDRIVE_SIDECAR_EXE").is_err()
}

fn source_backend_command() -> Command {
    if let Ok(python) = std::env::var("SOULDRIVE_PYTHON") {
        let mut command = Command::new(python);
        command.arg("-m").arg("core.sidecar_runtime");
        return command;
    }

    if cfg!(debug_assertions) {
        let mut command = Command::new("conda");
        command
            .arg("run")
            .arg("-n")
            .arg("souldrive")
            .arg("python")
            .arg("-m")
            .arg("core.sidecar_runtime");
        return command;
    }

    let mut command = Command::new("python");
    command.arg("-m").arg("core.sidecar_runtime");
    command
}

fn runtime_root<R: Runtime>(app: &impl Manager<R>) -> PathBuf {
    if let Ok(configured) = std::env::var("SOULDRIVE_APP_ROOT") {
        return PathBuf::from(configured);
    }

    for candidate in runtime_root_candidates(app) {
        if candidate.join("models").exists() || candidate.join("SoulDrive").exists() {
            return candidate;
        }
    }

    development_project_root()
}

fn sidecar_executable<R: Runtime>(
    app: &impl Manager<R>,
    runtime_root: &PathBuf,
) -> Option<PathBuf> {
    if let Ok(configured) = std::env::var("SOULDRIVE_SIDECAR_EXE") {
        let path = PathBuf::from(configured);
        if path.exists() {
            return Some(path);
        }
    }

    for candidate in sidecar_candidates(app, runtime_root) {
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}

fn sidecar_candidates<R: Runtime>(app: &impl Manager<R>, runtime_root: &PathBuf) -> Vec<PathBuf> {
    let executable_name = sidecar_executable_name();
    let mut roots = Vec::new();
    roots.push(runtime_root.clone());
    if cfg!(debug_assertions) {
        roots.push(development_sidecar_root());
    }
    if let Ok(resource_dir) = app.path().resource_dir() {
        roots.push(resource_dir);
    }
    if let Ok(exe_path) = std::env::current_exe() {
        if let Some(exe_dir) = exe_path.parent() {
            roots.push(exe_dir.to_path_buf());
        }
    }

    let mut candidates = Vec::new();
    for root in roots {
        candidates.push(root.join("sidecars").join(executable_name));
        candidates.push(
            root.join("sidecars")
                .join("souldrive-sidecar")
                .join(executable_name),
        );
    }
    candidates
}

fn development_sidecar_root() -> PathBuf {
    development_project_root()
        .join("souldrive-ui")
        .join("src-tauri")
}

#[cfg(windows)]
fn sidecar_executable_name() -> &'static str {
    "souldrive-sidecar.exe"
}

#[cfg(not(windows))]
fn sidecar_executable_name() -> &'static str {
    "souldrive-sidecar"
}

fn runtime_root_candidates<R: Runtime>(app: &impl Manager<R>) -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir);
    }
    if let Ok(exe_path) = std::env::current_exe() {
        if let Some(exe_dir) = exe_path.parent() {
            candidates.push(exe_dir.to_path_buf());
        }
    }
    candidates.push(development_project_root());
    candidates
}

fn development_project_root() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(|path| path.parent())
        .map(PathBuf::from)
        .unwrap_or(manifest_dir)
}

fn reserve_api_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let instance_guard = match acquire_single_instance() {
                Ok(guard) => guard,
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    rfd::MessageDialog::new()
                        .set_title("SoulDrive 已在运行")
                        .set_description("SoulDrive 已经打开。请使用现有窗口，不要重复启动。")
                        .set_level(rfd::MessageLevel::Info)
                        .show();
                    return Err(error.into());
                }
                Err(error) => return Err(error.into()),
            };
            app.manage(instance_guard);
            let token = generate_api_token();
            let api_port = reserve_api_port().unwrap_or(8000);
            let child = match spawn_backend(app, &token, api_port) {
                Ok(child) => {
                    wait_for_sidecar_ready(&token, api_port);
                    Some(child)
                }
                Err(error) => {
                    eprintln!("failed to start SoulDrive sidecar runtime: {error}");
                    None
                }
            };
            app.manage(BackendProcess {
                child: Mutex::new(child),
                api_token: token.clone(),
                api_port,
            });

            // Start sidecar health monitor in background
            let token_clone = token.clone();
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                sidecar_health_monitor(handle, token_clone, api_port);
            });

            #[cfg(windows)]
            start_ui_automation_hotkey(app.handle().clone());

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_status,
            runtime_config,
            skill_api_request,
            select_document_files,
            select_project_folder,
            capture_focused_ui_element,
            open_path,
            open_in_editor
        ])
        .build(tauri::generate_context!())
        .expect("error while building Tauri application");

    app.run(|handle, event| {
        if matches!(
            event,
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
        ) {
            handle.state::<BackendProcess>().shutdown();
        }
    });
}

fn wait_for_sidecar_ready(api_token: &str, api_port: u16) {
    let url = format!("http://127.0.0.1:{}/health", api_port);
    let client = reqwest::blocking::Client::new();
    for attempt in 0..30 {
        match client
            .get(&url)
            .header("X-SoulDrive-Token", api_token)
            .timeout(std::time::Duration::from_secs(2))
            .send()
        {
            Ok(response) if response.status().is_success() => {
                eprintln!("sidecar ready after {} attempts", attempt + 1);
                return;
            }
            _ => {
                std::thread::sleep(std::time::Duration::from_millis(500));
            }
        }
    }
    eprintln!("sidecar did not become ready within timeout");
}

fn sidecar_health_monitor(handle: tauri::AppHandle, api_token: String, api_port: u16) {
    let url = format!("http://127.0.0.1:{}/health", api_port);
    let client = reqwest::blocking::Client::new();
    let mut monitor_started = false;
    // Give conda/uvicorn and the local model process a full warm-up window.
    std::thread::sleep(std::time::Duration::from_secs(30));
    loop {
        std::thread::sleep(std::time::Duration::from_secs(8));
        if restart_if_child_exited(&handle, &api_token, api_port) {
            monitor_started = false;
            continue;
        }

        let healthy = client
            .get(&url)
            .header("X-SoulDrive-Token", &api_token)
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .map(|r| r.status().is_success())
            .unwrap_or(false);

        if healthy {
            monitor_started = true;
        } else if monitor_started {
            // Health is an observation signal only. The sidecar can block
            // briefly during local model/index work; restarting it from a
            // timeout destroys an otherwise healthy local session.
            eprintln!("sidecar health check failed; keeping process alive");
        }
    }
}

fn restart_if_child_exited(handle: &tauri::AppHandle, api_token: &str, api_port: u16) -> bool {
    let state = handle.state::<BackendProcess>();
    let Ok(mut guard) = state.child.lock() else {
        return false;
    };

    let Some(child) = guard.as_mut() else {
        drop(guard);
        restart_backend(handle, api_token, api_port, "sidecar process missing");
        return true;
    };

    match child.try_wait() {
        Ok(Some(_)) => {
            *guard = None;
            drop(guard);
            restart_backend(handle, api_token, api_port, "sidecar process exited");
            true
        }
        Ok(None) => false,
        Err(error) => {
            eprintln!("sidecar process probe failed: {error}");
            false
        }
    }
}

fn restart_backend(handle: &tauri::AppHandle, api_token: &str, api_port: u16, reason: &str) {
    eprintln!("sidecar restart requested: {reason}");
    let state = handle.state::<BackendProcess>();
    let lock_result = state.child.lock();
    if let Ok(mut guard) = lock_result {
        if let Some(mut child) = guard.take() {
            terminate_process_tree(&mut child);
        }
        match spawn_backend(handle, api_token, api_port) {
            Ok(new_child) => {
                wait_for_sidecar_ready(api_token, api_port);
                *guard = Some(new_child);
                eprintln!("sidecar restarted successfully");
            }
            Err(error) => {
                eprintln!("sidecar restart failed: {error}");
            }
        }
    }
}

fn generate_api_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).expect("secure random API unavailable");
    let token = bytes
        .iter()
        .map(|byte| format!("{:02x}", byte))
        .collect::<String>();
    format!("souldrive-{token}")
}

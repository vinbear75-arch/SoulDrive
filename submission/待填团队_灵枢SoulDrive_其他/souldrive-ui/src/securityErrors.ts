export type SecurityAction = "init" | "unlock";
export type SecurityPanelMode = "unavailable" | "initialize" | "unlocked" | "unlock";

interface SecurityPanelStatus {
  crypto_initialized: boolean;
  software_unlocked: boolean;
}

const backendErrorMessages: Record<string, string> = {
  "no recovery acknowledgement required": "请先确认忘记口令不可恢复",
  "workspace keystore already initialized": "该工作区已经初始化，请直接输入口令解锁",
  "workspace keystore is not initialized": "该工作区尚未初始化，请先设置口令",
  "incorrect passphrase": "口令错误，请重新输入",
};

export function securityPanelMode(status: SecurityPanelStatus | null): SecurityPanelMode {
  if (!status) return "unavailable";
  if (!status.crypto_initialized) return "initialize";
  return status.software_unlocked ? "unlocked" : "unlock";
}

export function formatSecurityActionError(
  action: SecurityAction,
  status: number,
  backendError?: string,
): string {
  if (status === 404) {
    return "当前本地 sidecar 版本不支持工作区解锁，请重新打包或更新本地服务。";
  }
  if (status === 401) {
    return "本地 API 认证失败，请重启桌面端后再试。";
  }
  if (status === 409 && action === "init") {
    return "该工作区已经初始化，请直接输入口令解锁。";
  }
  if (status === 409 && action === "unlock") {
    return "该工作区尚未初始化，请先设置口令。";
  }
  if (status === 403 && action === "unlock") {
    return "口令错误，请重新输入。";
  }

  const knownMessage = backendError ? backendErrorMessages[backendError] : undefined;
  if (knownMessage) {
    return `${knownMessage}。`;
  }
  if (backendError) {
    return `本地安全服务返回错误：${backendError}`;
  }
  if (status >= 500) {
    return "本地安全服务异常，请查看 sidecar 日志。";
  }

  return action === "init" ? "初始化失败，请检查本地服务状态。" : "解锁失败，请检查本地服务状态。";
}

export async function readSecurityActionError(
  action: SecurityAction,
  response: Response,
): Promise<string> {
  let backendError: string | undefined;
  try {
    const payload = (await response.json()) as { error?: unknown };
    if (typeof payload.error === "string") {
      backendError = payload.error;
    }
  } catch {
    backendError = undefined;
  }

  return formatSecurityActionError(action, response.status, backendError);
}

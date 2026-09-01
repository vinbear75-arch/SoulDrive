export type ChatTransportFailure = {
  timedOut: boolean;
  sidecarReachable: boolean;
  receivedOutput: boolean;
};

export function formatChatTransportFailure(failure: ChatTransportFailure): string {
  if (failure.timedOut) {
    return "模型超时：90 秒内未收到模型输出。请稍后重试，或缩短问题和上下文。";
  }
  if (!failure.sidecarReachable) {
    return "后端未启动：SoulDrive sidecar 当前不可访问。请检查启动终端是否仍在运行。";
  }
  if (failure.receivedOutput) {
    return "后端忙：模型生成过程中连接中断。请等待当前任务结束后重试。";
  }
  return "后端忙：sidecar 正在处理模型或索引任务，暂时无法开始本次生成。请稍后重试。";
}

export function formatChatHttpFailure(status: number, detail?: string): string {
  const reason = detail?.trim();
  if (status === 401) return "请求失败：本地 API 认证已失效，请重启 SoulDrive。";
  if (status === 422) return "请求失败：本轮输入超过服务限制，已停止处理。请缩短问题或重新发起提问。";
  if (status === 423) return "请求失败：当前工作区已锁定，请先解锁。";
  if (status === 503) return `后端忙：${reason || "本地服务尚未就绪，请稍后重试。"}`;
  if (status >= 500) return `请求失败：sidecar 返回服务端错误（${status}）。${reason ? ` ${reason}` : ""}`;
  return `请求失败：sidecar 拒绝了请求（${status}）。${reason ? ` ${reason}` : ""}`;
}

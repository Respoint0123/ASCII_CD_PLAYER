"""asciicd 全局常量 —— 所有可调参数集中在这里。"""
from __future__ import annotations

# ── 元信息 ────────────────────────────────────────────────────────────────
VERSION = "0.1.1"             # 与 pyproject.toml / 包名 / tag 保持一致
PROJECT_URL = "https://github.com/Respoint0123/ASCII_CD_PLAYER"

# ── 音频 ──────────────────────────────────────────────────────────────────
SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLES_PER_SECTOR = 588                      # 1 扇区 = 1/75 s
BYTES_PER_FRAME = CHANNELS * 2                # s16le 立体声
BYTES_PER_SEC = SAMPLE_RATE * BYTES_PER_FRAME  # 176 400
CD_SECTOR_BYTES = SAMPLES_PER_SECTOR * BYTES_PER_FRAME  # 2352

PCM_FORMAT_VERSION = 2        # 缓存 PCM 格式版本：2 = s16le（cdparanoia -r）
FEED_CHUNK = 16384            # 每次喂给 pw-cat 的字节数
PIPE_BYTES = 16384            # 管道容量，调小可降低延迟
OUTPUT_LATENCY_S = 0.15       # 播放头补偿：pipe 内 + PipeWire 节点缓冲

# ── 可视化 ────────────────────────────────────────────────────────────────
FPS = 30
N_FFT = 8192
SPECTRUM_BINS = 64
F_MIN = 25.0
F_MAX = 17000.0
SPEC_ATTACK = 0.62            # 上升：新值权重
SPEC_FALL = 0.84              # 下落：每帧保留比例
PEAK_FALL = 0.30              # 峰值标记每帧下落（占可视高度比例）
WATERFALL_ROWS = 4

# ── 抓轨 ──────────────────────────────────────────────────────────────────
PREROLL_S = 6.0               # 抓到这么多秒就开播
PREROLL_TIMEOUT_S = 20.0      # 但最多等这么久
TRACK_TRANSITION_S = 0.60     # 切轨时进度条/标题过渡时长
TRACK_HINT_S = 1.05           # 切轨徽标在标题栏停留时长
SEEK_PULSE_S = 0.22           # Seek 即时提示（150–250ms，不伪造播放头）
SEEK_DEBOUNCE_S = 0.18        # 连续 ←/→ 的 trailing-edge 合并窗口（150–200ms）
SEEK_HOLD_GAP_S = 0.32        # 两次 Seek 间隔小于此值视为长按的重复键
SEEK_HOLD_STEP_S = 0.08       # 单次重复最多累计的按住时长（平滑 OS 首次重复延迟）
SEEK_HOLD_MAX_S = 0.40        # 长按累计上限；达到后稳定显示 3 个箭头
SEEK_ARROW_2_S = 0.14         # 累计按住多久长出第 2 个箭头
SEEK_ARROW_3_S = 0.30         # 累计按住多久长出第 3 个箭头
SEEK_HOLD_RELEASE_S = 0.16    # 松开后箭头从 3 回落到 1 的时间
SEEK_HOLD_TAIL_S = 0.18       # 回落后提示再保留的时间
OUTPUT_QUICK_STOP_S = 0.20    # Seek 重启输出时等旧 pw-cat 退出的上限；超时直接 kill
STATE_PULSE_S = 0.13          # Pause/Resume 即时提示（80–150ms）
RIP_RETRIES = 5               # cdparanoia -z
RIP_PRIO_DEBOUNCE_S = 0.30    # 连续选曲时等目标稳定再提交，减少光头反复寻道
RIP_ATTEMPTS = 3              # 进程级重试：同一轨连续失败这么多次就放弃
RIP_SHORT_TOLERANCE = 4096    # 允许 cdparanoia 比 TOC 少写一点，落盘前补齐/截断对齐
RIP_SPEED_LIMIT = None        # cdparanoia -S（None = 不限速）
RIP_FAST = False              # True = cdparanoia -Y（少一遍校验，首字节 2.6s vs 6.9s）
# 抓轨范围（硬盘写入量）：disc=整盘预抓；track=只抓正在播的这一轨；
# next=本轨 + 下一轨（默认，换轨不卡又不白写整盘）
RIP_MODE = "next"
RIP_MODES = ("disc", "track", "next")
CACHE_DIR = "~/.cache/asciicd"
META_VERSION = 1              # 元数据缓存版本：查询算法变了就 +1，旧 meta.json 自动作废重查

# ── 换盘 ──────────────────────────────────────────────────────────────────
DISC_POLL_S = 0.1             # 托盘状态探测间隔（秒）；开仓动画尽快响应，ioctl 本身很轻
DISC_UNREADABLE_S = 2.0       # 有盘但暂时认不出 CD-DA 的重试宽限；超时后 UNREADABLE
EJECT_ACTION = "stop"         # 开仓立即停歌；continue 策略已取消
INSERT_ACTION = "pause"       # 插盘/启动默认停在 stopped，按空格才抓轨+播放；play=自动开播
# --on-eject 仅保留 stop；--on-insert 仍可覆盖

# ── 主题 ──────────────────────────────────────────────────────────────────
# 语义色：spec_lo/hi 频谱渐变两端，scope 波形，text 正文，dim 次要，
#         accent 高亮，warn 告警，err 错误，bar/bg_bar 进度条
THEMES: dict[str, dict[str, tuple[int, int, int]]] = {
    "phosphor": dict(
        spec_lo=(30, 150, 80), spec_hi=(205, 255, 170),
        scope=(90, 255, 150), text=(190, 245, 205), dim=(78, 138, 104),
        accent=(250, 240, 150), warn=(255, 190, 70), err=(255, 95, 85),
        bar=(95, 225, 135), bg_bar=(28, 58, 40),
    ),
    "amber": dict(
        spec_lo=(170, 80, 20), spec_hi=(255, 235, 160),
        scope=(255, 200, 90), text=(255, 225, 170), dim=(150, 105, 50),
        accent=(255, 255, 210), warn=(255, 170, 60), err=(255, 100, 80),
        bar=(255, 190, 70), bg_bar=(60, 40, 18),
    ),
    "jpop": dict(
        spec_lo=(255, 110, 170), spec_hi=(120, 230, 255),
        scope=(255, 170, 225), text=(255, 232, 242), dim=(170, 140, 190),
        accent=(130, 245, 255), warn=(255, 205, 120), err=(255, 90, 120),
        bar=(255, 140, 200), bg_bar=(56, 30, 66),
    ),
    "modern": dict(
        spec_lo=(55, 130, 255), spec_hi=(255, 90, 215),
        scope=(130, 255, 220), text=(225, 235, 255), dim=(105, 120, 150),
        accent=(130, 255, 200), warn=(255, 195, 75), err=(255, 85, 95),
        bar=(120, 190, 255), bg_bar=(35, 45, 70),
    ),
}
DEFAULT_THEME = "phosphor"

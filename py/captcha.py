# -*- coding: utf-8 -*-
"""
滑块验证码处理。
核心：缺口定位 loc_gap —— 用「拼图 alpha 剪影轮廓」与「背景 Sobel 梯度图」做形状边缘相关，
     找轮廓落在强边缘上的最优位置（即缺口），返回拼图在背景图中的左/上像素坐标。
坑点：不要用颜色/方差/内容模板匹配定位缺口——会受收据纸、反光、缺口处内容不同干扰，已验证不可靠。
"""
import io
import time
import random
import threading
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image

# 从“可见”的验证码容器读取当前显示的拼图（base64 返回）。避免用 API 重新取图导致 token 与显示的图不一致。
JS_GET_IMAGES = r"""
async () => {
  const conts = [...document.querySelectorAll('.slide-captcha-container')];
  const vis = conts.find(el => el.getBoundingClientRect().width > 0 && getComputedStyle(el).display !== 'none');
  if (!vis) return { err: 'no visible captcha' };
  const big = vis.querySelector('#bigImg') || vis.querySelector('.bg-block img') || vis.querySelector('img');
  const small = vis.querySelector('#smallImg');
  const block = vis.querySelector('#blockRef');
  const handle = vis.querySelector('#slideBlockRef');
  const toB64 = async (u) => {
    const r = await fetch(u); const b = await r.arrayBuffer();
    const bytes = new Uint8Array(b); let s = ''; const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH) s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    return btoa(s);
  };
  const br = big.getBoundingClientRect(), hr = handle.getBoundingClientRect();
  return {
    bigB64: await toB64(big.src), smallB64: await toB64(small.src),
    bigCssW: br.width, blockLeft: parseFloat(block.style.left || '0'),
    handleCx: hr.left + hr.width / 2, handleCy: hr.top + hr.height / 2
  };
}
"""


def loc_gap(bg_bytes, sl_bytes):
    """
    返回 (x, y, score)：拼图在背景图中应放置的左上角像素坐标与匹配得分。
    score 越大越可信；历史上最优位置与次优差值越大越稳（正常 >2 倍）。
    """
    bg = Image.open(io.BytesIO(bg_bytes)).convert('RGBA')
    sl = Image.open(io.BytesIO(sl_bytes)).convert('RGBA')
    bga = np.asarray(bg, dtype=np.float32)
    W, H = bg.size

    # Sobel 梯度幅值（float32 减半内存带宽，避免多线程下抢 CPU/内存）
    gray = 0.299 * bga[:, :, 0] + 0.587 * bga[:, :, 1] + 0.114 * bga[:, :, 2]
    sx = gray[1:-1, 2:] - gray[1:-1, :-2]
    sy = gray[2:, 1:-1] - gray[:-2, 1:-1]
    E = np.sqrt(sx ** 2 + sy ** 2)
    Efull = np.zeros((H, W), dtype=np.float32)
    Efull[1:-1, 1:-1] = E

    # 拼图 alpha 剪影 -> 轮廓点（只取边界，形状敏感）
    sla = np.asarray(sl)
    mask = sla[:, :, 3] > 128
    sw, sh = sl.size
    contour = []
    for yy in range(1, sh - 1):
        for xx in range(1, sw - 1):
            if mask[yy, xx] and not (mask[yy, xx - 1] and mask[yy, xx + 1] and mask[yy - 1, xx] and mask[yy + 1, xx]):
                contour.append((xx, yy))
    if len(contour) > 200:
        contour = contour[::len(contour) // 200]

    bh, bw = H - sh, W - sw
    if bh <= 0 or bw <= 0 or not contour:
        return 0, 0, 0.0

    # 向量化：对每个轮廓偏移累加梯度图，等价于轮廓对齐边缘的强度
    score = np.zeros((bh, bw), dtype=np.float32)
    for dx, dy in contour:
        score += Efull[dy:dy + bh, dx:dx + bw]
    score /= len(contour)

    idx = int(np.argmax(score))
    yidx, xidx = np.unravel_index(idx, score.shape)
    return int(xidx), int(yidx), float(score[yidx, xidx])


_POOL = {'executor': None, 'lock': threading.Lock(), 'broken': False}


def _process_pool():
    """懒加载单进程池：CPU 密集的缺口计算不占线程池工作线程。"""
    with _POOL['lock']:
        if _POOL['executor'] is None and not _POOL['broken']:
            try:
                _POOL['executor'] = ProcessPoolExecutor(max_workers=1)
            except Exception:
                _POOL['broken'] = True
        return _POOL['executor']


def loc_gap_offload(bg_bytes, sl_bytes, timeout=12.0):
    """把缺口识别放独立进程执行；进程池不可用/超时时回退当前线程。"""
    pool = _process_pool()
    if pool is not None:
        try:
            return pool.submit(loc_gap, bg_bytes, sl_bytes).result(timeout=timeout)
        except Exception:
            _POOL['broken'] = True
    return loc_gap(bg_bytes, sl_bytes)


def human_drag(page, dist, cx, cy):
    """模拟真人拖动：带加速度(先快后慢)、抖动与停顿，让页面自己加密提交轨迹。"""
    page.mouse.move(cx, cy)
    time.sleep(0.12 + random.random() * 0.18)
    page.mouse.down()
    time.sleep(0.06 + random.random() * 0.09)
    steps = 28 + random.randint(0, 10)
    for i in range(1, steps + 1):
        t = i / steps
        ease = 1 - (1 - t) ** 2.2
        nx = cx + dist * ease + (random.random() * 2 - 1) * 1.6
        ny = cy + (random.random() * 2 - 1) * 1.5
        page.mouse.move(nx, ny)
        time.sleep(0.008 + random.random() * 0.02)
    page.mouse.move(cx + dist, cy, steps=5)
    time.sleep(0.18 + random.random() * 0.2)
    page.mouse.up()

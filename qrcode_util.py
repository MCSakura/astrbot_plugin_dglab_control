# -*- coding: utf-8 -*-
"""二维码图片生成工具。

负责把 DG-LAB APP 配对 URL 渲染成 PNG 文件，供 AstrBot 直接以图片形式
发到群里给用户扫码连接。
"""

import os

import qrcode


def make_qrcode_image(url: str, save_path: str) -> str:
    """生成一张 PNG 二维码图片并保存到 save_path。

    Args:
        url: 要编码到二维码里的目标 URL（一般是 DG-LAB 跳转链接）。
        save_path: PNG 文件保存路径，包含文件名。

    Returns:
        实际写入磁盘的 PNG 文件绝对路径。
    """
    os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(save_path, format="PNG")
    return os.path.abspath(save_path)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SPK 自动构建脚本。

读取同目录下的 app.json，跟随上游 userdocs/qbittorrent-nox-static 的最新正式
Release，把官方的 qbittorrent-nox 静态二进制封装成群晖 DSM 7 可安装的 .spk，
并输出构建清单。

版本号规则（与矿神 SPK 一致，群晖只接受简单版本号）：
    上游 qBittorrent 主版本一致时，横线后的构建序号 1、2、3 … 递增；
    主版本变化（如 5.3.0 -> 5.4.0）时，构建序号重置为 1。
    例：5.3.0-1 -> 5.3.0-2（上游仅更新 libtorrent 时）-> 5.4.0-1（新主版本）

已构建的上游 tag 记录在 Release 正文的机器可读标记里，因此流水线可以精确
幂等：同一上游 Release 不会重复打包，也不会把同一个版本号用两次。
"""

import hashlib
import json
import os
import re
import shutil
import struct
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from spk_builder import create_spk

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
TEMPLATE = ROOT / "template"
API = "https://api.github.com"

# Release 正文中的机器可读标记。构建器写入，下次运行据此判断「这个上游 tag 已打包」。
UPSTREAM_MARKER = "<!-- upstream-tag: {tag} -->"

# 上游 tag 形如 release-5.3.0_v2.1.2（qBittorrent 版本 _ libtorrent 版本）
TAG_RE = re.compile(r"^release-(?P<qbt>[0-9][0-9A-Za-z.\-]*?)_v(?P<lt>[0-9][0-9A-Za-z.\-]*)$")

# ELF e_machine 期望值，用于校验下载到的二进制架构是否与目标一致
ELF_MACHINE = {
    "x86_64": 0x3E,   # EM_X86_64
    "arm": 0xB7,      # EM_AARCH64
}

# 群晖版本号只允许数字与点，构建序号用横线连接：<主版本>-<序号>
VERSION_RE = re.compile(r"^(?P<base>[0-9][0-9.]*)-(?P<build>[0-9]+)$")


def token():
    return os.environ.get("GITHUB_TOKEN", "").strip()


def get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "spk-builder",
        "Accept": "application/vnd.github+json",
    })
    if token():
        req.add_header("Authorization", "Bearer " + token())
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def get_all_releases(repo):
    """分页拉取全部 Release（用于读取历史构建记录）。"""
    out = []
    page = 1
    while page <= 20:
        batch = get_json("{0}/repos/{1}/releases?per_page=100&page={2}".format(API, repo, page))
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "spk-builder"})
    if token():
        req.add_header("Authorization", "Bearer " + token())
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=900) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)
    return Path(dest)


def md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def match_asset(assets, pattern):
    rx = re.compile(pattern)
    for asset in assets:
        if rx.search(asset.get("name", "")):
            return asset
    return None


def parse_upstream_tag(tag):
    """解析上游 tag，返回 (qBittorrent 版本, libtorrent 版本)。"""
    m = TAG_RE.match((tag or "").strip())
    if not m:
        raise RuntimeError(
            "无法解析上游 tag 版本号: {0!r}（期望形如 release-5.3.0_v2.1.2）".format(tag))
    return m.group("qbt"), m.group("lt")


def assert_binary_arch(path, family):
    """校验下载到的二进制确实是目标架构，避免把错架构的包发给用户。"""
    with open(path, "rb") as fh:
        head = fh.read(20)
    if len(head) < 20 or head[:4] != b"\x7fELF":
        raise RuntimeError("上游产物不是 ELF 可执行文件，无法打包: " + str(path))
    machine = struct.unpack("<H", head[18:20])[0]
    want = ELF_MACHINE[family]
    if machine != want:
        raise RuntimeError(
            "二进制架构不符: 期望 e_machine=0x{0:02x}（{1}），实际 0x{2:02x}".format(
                want, family, machine))


def scan_published(releases):
    """从全部 Release 中读出已构建的成果。

    返回 (已打包的上游 tag 集合, {主版本: 已用最大构建序号})
    """
    built_tags = set()
    max_build = {}
    for rel in releases:
        body = rel.get("body") or ""
        for m in re.finditer(r"<!--\s*upstream-tag:\s*(\S+?)\s*-->", body):
            built_tags.add(m.group(1))
        tag = (rel.get("tag_name") or "").strip()
        if "-v" not in tag:
            continue
        version = tag.rsplit("-v", 1)[1]
        vm = VERSION_RE.match(version)
        if not vm:
            continue
        base = vm.group("base")
        build = int(vm.group("build"))
        if build > max_build.get(base, 0):
            max_build[base] = build
    return built_tags, max_build


def spk_name(cfg, version, family):
    return "{0}_{1}_{2}.spk".format(
        cfg["package_id"], version, "armv8" if family == "arm" else "x86_64")


def build_one(cfg, arch_cfg, version, upstream_tag, lt_version, assets, workdir):
    family = arch_cfg["family"]
    info_arch = "armv8" if family == "arm" else "x86_64"
    out_path = DIST / spk_name(cfg, version, family)

    asset = match_asset(assets, arch_cfg["asset_pattern"])
    if not asset:
        raise RuntimeError("未找到匹配的 Release 资产: " + arch_cfg["asset_pattern"])

    binary_name = arch_cfg.get("binary_path", "qbittorrent-nox")
    src = download(asset["browser_download_url"], Path(workdir) / asset["name"])
    assert_binary_arch(src, family)

    changelog = "更新 qBittorrent 到 v{0}，libtorrent 为 v{1}（官方标准版，非 Enhanced）。".format(
        version.split("-")[0], lt_version)

    with open(src, "rb") as fh:
        binary = fh.read()
    return create_spk(
        output_spk_path=str(out_path),
        package_id=cfg["package_id"],
        version=version,
        display_name=cfg["display_name"],
        description=cfg["description"],
        description_chs=cfg["description_chs"],
        maintainer=cfg["maintainer"],
        maintainer_url=cfg["maintainer_url"],
        distributor=cfg["distributor"],
        distributor_url=cfg["distributor_url"],
        arch=info_arch,
        binary_content=binary,
        binary_filename=binary_name,
        dsm_ver=cfg["os_min_ver"],
        changelog=changelog,
        template_dir=str(TEMPLATE),
        extra_items=None,
    )


def main():
    cfg = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()

    # 上游最新「正式」Release（忽略 prerelease / draft）。
    # userdocs 仓库会把 libtorrent v1.2 / v2.0 分支的构建标成 prerelease，
    # 因此取 latest 的正式版即默认的 libtorrent v2.1 主线，符合「标准官方版」要求。
    try:
        rel = get_json("{0}/repos/{1}/releases/latest".format(API, cfg["upstream_repo"]))
    except Exception:
        rel = None
    if not rel or rel.get("prerelease") or rel.get("draft"):
        all_rel = get_json(
            "{0}/repos/{1}/releases?per_page=100".format(API, cfg["upstream_repo"]))
        rel = next((r for r in all_rel
                    if not r.get("prerelease") and not r.get("draft")), None)
    if not rel:
        raise RuntimeError("上游未找到任何正式 Release，无法构建。")

    tag = (rel.get("tag_name") or "").strip()
    qbt_version, lt_version = parse_upstream_tag(tag)
    print("上游最新: {0}  =>  qBittorrent {1} / libtorrent {2}".format(
        tag, qbt_version, lt_version))

    # 从本仓库历史 Release 推导「下一个版本号」并判断是否需要构建。
    built_tags, max_build = set(), {}
    if repo:
        try:
            built_tags, max_build = scan_published(get_all_releases(repo))
        except Exception as exc:
            print("[!] 读取历史 Release 失败（将视为全新构建）: {0}".format(exc))

    force = bool(os.environ.get("FORCE_REBUILD"))
    if tag in built_tags and not force:
        print("[=] 上游 {0} 已打包发布，跳过。".format(tag))
        return 0

    # 主版本一致 -> 构建序号 +1；主版本变化 -> 从 1 开始
    next_build = max_build.get(qbt_version, 0) + 1
    # 若同版本号因强制重建被复用，FYI 打印出来便于排查
    version = "{0}-{1}".format(qbt_version, next_build)
    print("打包版本: {0}（主版本 {1} 的第 {2} 次构建）".format(
        version, qbt_version, next_build))

    DIST.mkdir(exist_ok=True)
    for stale in DIST.glob("*.spk"):
        stale.unlink()

    assets_out = []
    workdir = Path(os.environ.get("TMPDIR", "/tmp")) / "spk-build-qbt"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        for arch_cfg in cfg["architectures"]:
            name = spk_name(cfg, version, arch_cfg["family"])
            print("构建 " + name)
            md5 = build_one(cfg, arch_cfg, version, tag, lt_version,
                            rel.get("assets", []), workdir)
            assets_out.append({
                "file": name,
                "md5": md5,
                "family": arch_cfg["family"],
            })
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    manifest = {
        "package": cfg["package_id"],
        "version": version,
        "upstream_repo": cfg["upstream_repo"],
        "upstream_tag": tag,
        "upstream_qbittorrent": qbt_version,
        "upstream_libtorrent": lt_version,
        "os_min_ver": cfg["os_min_ver"],
        "assets": assets_out,
    }
    (DIST / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 正文格式与同流水线的其他套件仓库保持一致：
    #   上游版本 / 打包版本 / 最低 DSM 版本，空行，然后 MD5 表格。
    # 上游 tag 形如 release-5.3.0_v2.1.2，同时含 qBittorrent 与 libtorrent
    # 两个版本，因此一并标出，避免只看 5.3.0 无法判断 libtorrent 版本。
    lines = [
        "上游版本：{0}（libtorrent {1}）".format(qbt_version, lt_version),
        "打包版本：" + version,
        "最低 DSM 版本：" + cfg["os_min_ver"],
        "",
        "| 文件 | 架构 | MD5 |",
        "| --- | --- | --- |",
    ]
    for item in assets_out:
        lines.append("| `{0}` | {1} | `{2}` |".format(
            item["file"], "ARM" if item["family"] == "arm" else "x86_64", item["md5"]))
    # ★ 标记放在正文末尾：套件源流水线取正文前 300 字符作为套件中心的
    #   changelog，标记若在开头会变成可见文本。放在末尾既不影响 changelog，
    #   渲染时 HTML 注释也不可见。
    lines += ["", UPSTREAM_MARKER.format(tag=tag)]
    (DIST / "notes.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("构建完成，产物:")
    for item in assets_out:
        print("  {0}  {1}".format(item["file"], item["md5"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

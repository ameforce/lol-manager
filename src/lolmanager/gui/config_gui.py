from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from lolmanager.core.champion_config import ChampionConfig
from lolmanager.core.opgg_champion_list import (
    build_normalized_index,
    default_cache_path,
    get_champion_dataset,
    load_champion_list_cache,
    normalize_name,
)
from lolmanager.core.opgg_counter_recommendations import (
    DEFAULT_MAX_AGE_SEC as COUNTER_RECOMMENDATION_MAX_AGE_SEC,
    build_label_name_map,
    default_counter_cache_path,
    get_counter_recommendations,
    load_recommendation_cache,
)
from lolmanager.platform.paths import champion_config_path, resource_path
from lolmanager.platform.runtime import is_frozen


if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import ttk


ROLE_ORDER: Tuple[str, ...] = ("top", "jungle", "mid", "adc", "support")
ROLE_LABEL_KO: Dict[str, str] = {
    "top": "탑",
    "jungle": "정글",
    "mid": "미드",
    "adc": "원딜",
    "support": "서폿",
}
APP_USER_MODEL_ID = "LOLManager"
DISPLAY_SEPARATOR_PREFIX = "────────"


def display_value_to_champion_name(
    value: str,
    *,
    label_to_name: Optional[Dict[str, str]] = None,
) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if label_to_name and s in label_to_name:
        return str(label_to_name[s] or "").strip()
    if s.startswith(DISPLAY_SEPARATOR_PREFIX):
        return ""

    dot = s.find(". ")
    if dot > 0 and s[:dot].strip().isdigit():
        return s[dot + 2 :].strip()
    return s


def _set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> None:
    if os.name != "nt":
        return

    if is_frozen():
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
    except Exception:
        pass


@dataclass
class _RoleVars:
    champion: "tk.StringVar"
    ban: "tk.StringVar"
    pick_x: "tk.StringVar"
    pick_y: "tk.StringVar"
    reserve1_champion: "tk.StringVar"
    reserve1_ban: "tk.StringVar"
    reserve2_champion: "tk.StringVar"
    reserve2_ban: "tk.StringVar"


@dataclass
class _RoleWidgets:
    champion_cb: "ttk.Combobox"
    ban_cb: "ttk.Combobox"
    reserve1_champion_cb: "ttk.Combobox"
    reserve1_ban_cb: "ttk.Combobox"
    reserve2_champion_cb: "ttk.Combobox"
    reserve2_ban_cb: "ttk.Combobox"


def _parse_int_pair(x: str, y: str) -> Optional[Tuple[int, int]]:
    xs = str(x or "").strip()
    ys = str(y or "").strip()
    if not xs and not ys:
        return None
    if not xs or not ys:
        return None
    try:
        xi = int(xs)
        yi = int(ys)
    except ValueError:
        return None
    return (xi, yi)


def _normalize_reserves(
    primary: str, raw: List[Tuple[str, str]]
) -> List[Dict[str, str]]:
    primary_cf = str(primary or "").strip().casefold()
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for champ, ban in raw:
        c = str(champ or "").strip()
        if not c:
            continue
        if c.casefold() == primary_cf:
            continue
        key = c.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({"champion": c, "ban": str(ban or "").strip()})
        if len(out) >= 2:
            break
    return out


def run_config_gui(config_path: Optional[Path] = None) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
        from tkinter import font as tkfont
    except Exception as exc:
        raise RuntimeError(
            "tkinter를 import할 수 없습니다. Python 설치에 Tk 지원이 필요합니다."
        ) from exc

    config = ChampionConfig(path=(config_path or champion_config_path()))
    counter_cache_path = default_counter_cache_path(config.path.resolve())

    _set_app_user_model_id()
    root = tk.Tk()
    root.title("LOLManager 설정")
    root.minsize(720, 480)

    try:
        family = "맑은 고딕"
        tkfont.nametofont("TkDefaultFont").configure(family=family, size=10)
        tkfont.nametofont("TkTextFont").configure(family=family, size=10)
        tkfont.nametofont("TkHeadingFont").configure(
            family=family, size=10, weight="bold"
        )
    except Exception:
        pass
    try:
        ico = resource_path("assets", "lolmanager.ico")
        if ico.exists():
            root.iconbitmap(str(ico))
    except Exception:
        pass

    try:
        from lolmanager.gui.ui_theme import apply_modern_theme

        apply_modern_theme(root)
    except Exception:
        pass

    top = ttk.Frame(root, padding=10)
    top.pack(side=tk.TOP, fill=tk.X)

    ttk.Label(
        top,
        text=(
            "설정 변경은 여기서만 수행하세요.\n"
            "저장은 설정 파일에만 반영되며, 실제 적용은 메인 앱(LOLManager)에서 Stop→Start 또는 재실행 후 반영됩니다."
        ),
        wraplength=900,
    ).pack(side=tk.TOP, anchor=tk.W)

    path_row = ttk.Frame(top)
    path_row.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
    ttk.Label(path_row, text="설정 파일:").pack(side=tk.LEFT)
    cfg_path_str = str(config.path.resolve())
    ttk.Label(path_row, text=cfg_path_str).pack(side=tk.LEFT, padx=(6, 0))

    body = ttk.Frame(root, padding=10)
    body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    notebook = ttk.Notebook(body)
    notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    role_vars: Dict[str, _RoleVars] = {}
    champion_combos: List["ttk.Combobox"] = []
    champion_index: Dict[str, str] = {}
    all_champion_values: List[str] = []
    role_widgets: Dict[str, _RoleWidgets] = {}

    role_ranked_entries: Dict[str, List[Tuple[str, str, str]]] = {}
    role_href_index: Dict[str, Dict[str, str]] = {}
    counter_cache: Dict[Tuple[str, str], Tuple[List[str], Dict[str, str], str]] = {}
    ban_label_to_name: Dict[str, str] = {}
    _ban_update_after: Dict[str, str] = {}
    _ban_update_token: Dict[str, int] = {}
    tab_to_role: Dict[str, str] = {}
    _last_valid_raw: Dict[str, str] = {}

    _SEP_PREFIX = DISPLAY_SEPARATOR_PREFIX

    def _is_separator_value(value: str) -> bool:
        return str(value or "").strip().startswith(_SEP_PREFIX)

    def _display_to_champion_name(value: str) -> str:
        s = str(value or "").strip()
        if not s:
            return ""
        if _is_separator_value(s):
            return ""

        return display_value_to_champion_name(s, label_to_name=ban_label_to_name)

    def _refine_selected_to_name(field_key: str, var: "tk.StringVar") -> None:
        raw = str(var.get() or "").strip()
        if not raw:
            _last_valid_raw[field_key] = ""
            return
        if _is_separator_value(raw):
            prev_raw = _last_valid_raw.get(field_key, "")
            if prev_raw != raw:
                var.set(prev_raw)
            return

        name = _display_to_champion_name(raw)
        if not name:
            var.set("")
            _last_valid_raw[field_key] = ""
            return

        canonical = (
            champion_index.get(normalize_name(name), name) if champion_index else name
        )
        _last_valid_raw[field_key] = canonical
        if canonical != raw:
            var.set(canonical)

    def _add_labeled_entry(
        parent, row: int, label: str, var: "tk.StringVar", width: int = 26
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4
        )
        ent = ttk.Entry(parent, textvariable=var, width=width)
        ent.grid(row=row, column=1, sticky="we", pady=4)

    def _add_labeled_champion_combo(
        parent, row: int, label: str, var: "tk.StringVar", width: int = 26
    ) -> "ttk.Combobox":
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4
        )
        cb = ttk.Combobox(parent, textvariable=var, width=width, state="normal")
        cb.grid(row=row, column=1, sticky="we", pady=4)
        champion_combos.append(cb)
        return cb

    def _add_coord_row(
        parent, row: int, vx: "tk.StringVar", vy: "tk.StringVar"
    ) -> None:
        ttk.Label(parent, text="pick_coord (x, y)").grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4
        )
        coord = ttk.Frame(parent)
        coord.grid(row=row, column=1, sticky="w", pady=4)
        ttk.Entry(coord, textvariable=vx, width=10).pack(side=tk.LEFT)
        ttk.Label(coord, text=",").pack(side=tk.LEFT, padx=4)
        ttk.Entry(coord, textvariable=vy, width=10).pack(side=tk.LEFT)
        ttk.Label(coord, text="(비우면 기본값 사용)").pack(side=tk.LEFT, padx=(8, 0))

    for role in ROLE_ORDER:
        tab = ttk.Frame(notebook, padding=12)
        notebook.add(tab, text=ROLE_LABEL_KO.get(role, role))
        tab_to_role[str(tab)] = role

        tab.columnconfigure(1, weight=1)

        info = config.get(role) or {}
        champ = str(info.get("champion") or "").strip()
        ban = str(info.get("ban") or "").strip()
        coord = info.get("pick_coord") or None
        px = ""
        py = ""
        if isinstance(coord, (list, tuple)) and len(coord) >= 2:
            px = str(coord[0])
            py = str(coord[1])

        reserves = config.get_reserve_picks(role)
        r1c = str(reserves[0][0]).strip() if len(reserves) >= 1 else ""
        r1b = str(reserves[0][1]).strip() if len(reserves) >= 1 else ""
        r2c = str(reserves[1][0]).strip() if len(reserves) >= 2 else ""
        r2b = str(reserves[1][1]).strip() if len(reserves) >= 2 else ""

        rv = _RoleVars(
            champion=tk.StringVar(value=champ),
            ban=tk.StringVar(value=ban),
            pick_x=tk.StringVar(value=px),
            pick_y=tk.StringVar(value=py),
            reserve1_champion=tk.StringVar(value=r1c),
            reserve1_ban=tk.StringVar(value=r1b),
            reserve2_champion=tk.StringVar(value=r2c),
            reserve2_ban=tk.StringVar(value=r2b),
        )
        role_vars[role] = rv

        champ_cb = _add_labeled_champion_combo(
            tab, 0, "champion", rv.champion, width=34
        )
        ban_cb = _add_labeled_champion_combo(tab, 1, "ban", rv.ban, width=34)
        _add_coord_row(tab, 2, rv.pick_x, rv.pick_y)

        ttk.Separator(tab).grid(row=3, column=0, columnspan=2, sticky="we", pady=10)
        ttk.Label(tab, text="reserve_picks (최대 2개)").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=(0, 6)
        )

        r1c_cb = _add_labeled_champion_combo(
            tab, 5, "reserve1 champion", rv.reserve1_champion, width=34
        )
        r1b_cb = _add_labeled_champion_combo(
            tab, 6, "reserve1 ban", rv.reserve1_ban, width=34
        )
        ttk.Separator(tab).grid(row=7, column=0, columnspan=2, sticky="we", pady=10)
        r2c_cb = _add_labeled_champion_combo(
            tab, 8, "reserve2 champion", rv.reserve2_champion, width=34
        )
        r2b_cb = _add_labeled_champion_combo(
            tab, 9, "reserve2 ban", rv.reserve2_ban, width=34
        )

        champ_cb.bind(
            "<<ComboboxSelected>>",
            lambda _e, fk=f"{role}:primary", v=rv.champion: _refine_selected_to_name(
                fk, v
            ),
        )
        r1c_cb.bind(
            "<<ComboboxSelected>>",
            lambda _e,
            fk=f"{role}:reserve1",
            v=rv.reserve1_champion: _refine_selected_to_name(fk, v),
        )
        r2c_cb.bind(
            "<<ComboboxSelected>>",
            lambda _e,
            fk=f"{role}:reserve2",
            v=rv.reserve2_champion: _refine_selected_to_name(fk, v),
        )

        role_widgets[role] = _RoleWidgets(
            champion_cb=champ_cb,
            ban_cb=ban_cb,
            reserve1_champion_cb=r1c_cb,
            reserve1_ban_cb=r1b_cb,
            reserve2_champion_cb=r2c_cb,
            reserve2_ban_cb=r2b_cb,
        )

    bottom = ttk.Frame(root, padding=10)
    bottom.pack(side=tk.BOTTOM, fill=tk.X)

    status_var = tk.StringVar(value="대기 중")
    ttk.Label(bottom, textvariable=status_var).pack(side=tk.LEFT)

    btns = ttk.Frame(bottom)
    btns.pack(side=tk.RIGHT)

    def _build_display_values_for_role(role: str) -> List[str]:
        base = all_champion_values
        ranked = role_ranked_entries.get(role) or []
        if not ranked:
            return base

        out: List[str] = []
        ranked_keys: set[str] = set()
        last_tier: Optional[str] = None
        for rank, entry in enumerate(ranked, start=1):
            name, tier_label = entry[0], entry[1]
            n = str(name or "").strip()
            if not n:
                continue
            t = str(tier_label or "").strip() or "unknown"
            if t != last_tier:
                out.append(f"{_SEP_PREFIX} {t} {_SEP_PREFIX}")
                last_tier = t
            out.append(f"{rank:>3}. {n}")
            ranked_keys.add(normalize_name(n))

        tail = [x for x in base if normalize_name(x) not in ranked_keys]
        if tail:
            out.append(f"{_SEP_PREFIX} 기타 {_SEP_PREFIX}")
            out.extend(tail)
        return out

    def _filter_display_values(
        values: List[str], exclude_keys: set[str], keep_raw: str
    ) -> List[str]:
        keep_name = _display_to_champion_name(keep_raw)
        keep_key = normalize_name(keep_name) if keep_name else ""

        tmp: List[str] = []
        for v in values:
            if _is_separator_value(v):
                tmp.append(v)
                continue
            name = _display_to_champion_name(v)
            key = normalize_name(name)
            if keep_key and key == keep_key:
                tmp.append(v)
                continue
            if key in exclude_keys:
                continue
            tmp.append(v)

        cleaned: List[str] = []
        prev_sep = False
        for v in tmp:
            is_sep = _is_separator_value(v)
            if is_sep:
                if not cleaned or prev_sep:
                    prev_sep = True
                    continue
                cleaned.append(v)
                prev_sep = True
                continue
            cleaned.append(v)
            prev_sep = False
        while cleaned and _is_separator_value(cleaned[-1]):
            cleaned.pop()

        if keep_raw and keep_raw not in cleaned:
            cleaned.insert(0, keep_raw)
        return cleaned

    def _apply_ranked_values_for_role(role: str) -> None:
        if not all_champion_values:
            return
        w = role_widgets.get(role)
        if not w:
            return
        rv = role_vars.get(role)
        primary_raw = str(rv.champion.get() if rv else "").strip()
        r1_raw = str(rv.reserve1_champion.get() if rv else "").strip()
        r2_raw = str(rv.reserve2_champion.get() if rv else "").strip()
        primary = _display_to_champion_name(primary_raw)
        r1 = _display_to_champion_name(r1_raw)
        r2 = _display_to_champion_name(r2_raw)

        display_values = _build_display_values_for_role(role)

        try:
            w.champion_cb.configure(values=display_values)
        except Exception:
            pass

        ex_r1 = {normalize_name(primary), normalize_name(r2)}
        ex_r2 = {normalize_name(primary), normalize_name(r1)}
        try:
            w.reserve1_champion_cb.configure(
                values=_filter_display_values(display_values, ex_r1, r1_raw)
            )
        except Exception:
            pass
        try:
            w.reserve2_champion_cb.configure(
                values=_filter_display_values(display_values, ex_r2, r2_raw)
            )
        except Exception:
            pass

    def _set_role_dataset(
        by_position: Dict[str, List[Tuple[str, str, str]]], source: str
    ) -> None:
        role_ranked_entries.clear()
        role_href_index.clear()
        for role, entries in (by_position or {}).items():
            if not isinstance(role, str) or not entries:
                continue
            ranked: List[Tuple[str, str, str]] = []
            href_map: Dict[str, str] = {}
            for name, tier_label, href in entries:
                n = str(name or "").strip()
                t = str(tier_label or "").strip() or "unknown"
                h = str(href or "").strip()
                if not n:
                    continue
                ranked.append((n, t, h))
                if h:
                    href_map[normalize_name(n)] = h
            if ranked:
                role_ranked_entries[role] = ranked
            if href_map:
                role_href_index[role] = href_map

        for role in ROLE_ORDER:
            _apply_ranked_values_for_role(role)

        if by_position:
            status_var.set(f"op.gg 순위 데이터 반영 완료 ({source})")
            _refresh_configured_bans(allow_refresh=True)

    def _set_champion_list(names: List[str], source: str) -> None:
        nonlocal champion_index
        nonlocal all_champion_values

        values = sorted((str(n or "").strip() for n in names), key=normalize_name)
        values = [v for v in values if v]
        all_champion_values = values
        champion_index = build_normalized_index(values)
        for cb in champion_combos:
            try:
                cb.configure(values=values)
            except Exception:
                pass

        status_var.set(f"챔피언 목록 로드 완료 ({len(values)}개, {source})")

        for role in ROLE_ORDER:
            _apply_ranked_values_for_role(role)
        _refresh_configured_bans(allow_refresh=False)

    def _load_champion_list_async() -> None:
        cfg_path = config.path.resolve()
        cache_path = default_cache_path(cfg_path)

        cache = load_champion_list_cache(cache_path)
        if cache is not None:
            _set_champion_list(list(cache.champions), "cache")
            if getattr(cache, "by_position", None):
                try:
                    by_pos = {k: list(v) for k, v in cache.by_position.items()}
                except Exception:
                    by_pos = {}
                _set_role_dataset(by_pos, "cache")

        def _worker() -> None:
            try:
                names, by_position, source = get_champion_dataset(
                    cache_path, max_age_sec=7 * 24 * 60 * 60, timeout_sec=10.0
                )
            except Exception as exc:
                msg = f"챔피언 목록 로드 실패: {exc}"
                root.after(0, lambda: status_var.set(msg))
                return
            root.after(0, lambda: _set_champion_list(names, source))
            root.after(0, lambda: _set_role_dataset(by_position, source))

        status_var.set("챔피언 목록 불러오는 중...")
        threading.Thread(target=_worker, daemon=True).start()

    def _resolve_opgg_href(role: str, champion_name: str) -> Optional[str]:
        href_map = role_href_index.get(role)
        if href_map:
            href = href_map.get(normalize_name(champion_name))
            if href:
                return href

        try:
            from lolmanager.core.champion_fetcher import fetch_champion_slug
        except Exception:
            return None
        try:
            return fetch_champion_slug(role, champion_name)
        except Exception:
            return None

    def _build_recommendation_values(
        role: str,
        champion_name: str,
        *,
        allow_refresh: bool,
    ) -> Tuple[List[str], Dict[str, str], str]:
        ranked_entries = role_ranked_entries.get(role) or []
        if allow_refresh:
            result = get_counter_recommendations(
                counter_cache_path,
                role=role,
                configured_pick=champion_name,
                ranked_entries=ranked_entries,
                detail_href=_resolve_opgg_href(role, champion_name),
                max_age_sec=COUNTER_RECOMMENDATION_MAX_AGE_SEC,
            )
        else:
            result = load_recommendation_cache(
                counter_cache_path,
                role=role,
                configured_pick=champion_name,
                max_age_sec=COUNTER_RECOMMENDATION_MAX_AGE_SEC,
            )

        labels, label_to_name = build_label_name_map(result.recommendations)
        return (labels, label_to_name, result.status)

    def _set_ban_candidates(
        *,
        field_key: str,
        token: int,
        role: str,
        champion_name: str,
        ban_cb: "ttk.Combobox",
        ban_var: "tk.StringVar",
        labels: List[str],
        label_to_name: Dict[str, str],
        source: str,
    ) -> None:
        if _ban_update_token.get(field_key, 0) != token:
            return

        ban_label_to_name.update(label_to_name)
        values = labels if labels else all_champion_values
        try:
            ban_cb.configure(values=values)
        except Exception:
            pass

        if labels:
            current_name = _display_to_champion_name(str(ban_var.get() or ""))
            current_key = normalize_name(current_name) if current_name else ""
            current_label = ""
            if current_key:
                for label, name in label_to_name.items():
                    if normalize_name(name) == current_key:
                        current_label = label
                        break
            if current_label and str(ban_var.get() or "").strip() != current_label:
                ban_var.set(current_label)
            elif not current_name:
                ban_var.set(labels[0])

        role_label = ROLE_LABEL_KO.get(role, role)
        if labels:
            status_var.set(
                f"[{role_label}] 밴 후보 갱신 완료 ({len(labels)}개, {source})"
            )
        else:
            status_var.set(
                f"[{role_label}] 밴 후보를 가져오지 못했습니다(수동 입력 가능)"
            )

    def _schedule_ban_update(
        *,
        field_key: str,
        role: str,
        champion_var: "tk.StringVar",
        ban_cb: "ttk.Combobox",
        ban_var: "tk.StringVar",
        delay_ms: int = 450,
        allow_refresh: bool = False,
    ) -> None:
        _apply_ranked_values_for_role(role)

        prev = _ban_update_after.get(field_key)
        if prev:
            try:
                root.after_cancel(prev)
            except Exception:
                pass

        token = int(_ban_update_token.get(field_key, 0)) + 1
        _ban_update_token[field_key] = token

        def _kick() -> None:
            champ_raw = str(champion_var.get() or "").strip()
            if _is_separator_value(champ_raw):
                prev_raw = _last_valid_raw.get(field_key, "")
                if prev_raw and prev_raw != champ_raw:
                    champion_var.set(prev_raw)
                return

            champ_name_raw = _display_to_champion_name(champ_raw)
            if not champ_name_raw:
                try:
                    ban_cb.configure(values=all_champion_values)
                except Exception:
                    pass
                return
            _last_valid_raw[field_key] = champ_raw

            canon = champion_index.get(normalize_name(champ_name_raw))
            if champion_index and canon is None:
                try:
                    ban_cb.configure(values=all_champion_values)
                except Exception:
                    pass
                return

            champ = canon if canon else champ_name_raw

            if canon and canon != champ_name_raw and champ_raw == champ_name_raw:
                champion_var.set(canon)

            cache_key = (role, normalize_name(champ))
            cached = counter_cache.get(cache_key)
            if cached is not None and not allow_refresh:
                labels, label_to_name, source = cached
                _set_ban_candidates(
                    field_key=field_key,
                    token=token,
                    role=role,
                    champion_name=champ,
                    ban_cb=ban_cb,
                    ban_var=ban_var,
                    labels=labels,
                    label_to_name=label_to_name,
                    source=source,
                )
                return

            role_label = ROLE_LABEL_KO.get(role, role)
            status_var.set(f"[{role_label}] 밴 후보 불러오는 중...")

            def _worker() -> None:
                values = _build_recommendation_values(
                    role,
                    champ,
                    allow_refresh=allow_refresh,
                )
                counter_cache[cache_key] = values
                labels, label_to_name, source = values
                root.after(
                    0,
                    lambda: _set_ban_candidates(
                        field_key=field_key,
                        token=token,
                        role=role,
                        champion_name=champ,
                        ban_cb=ban_cb,
                        ban_var=ban_var,
                        labels=labels,
                        label_to_name=label_to_name,
                        source=source,
                    ),
                )

            threading.Thread(target=_worker, daemon=True).start()

        _ban_update_after[field_key] = root.after(delay_ms, _kick)

    def _refresh_configured_bans(*, allow_refresh: bool) -> None:
        for role in ROLE_ORDER:
            rv = role_vars.get(role)
            w = role_widgets.get(role)
            if not rv or not w:
                continue
            _schedule_ban_update(
                field_key=f"{role}:primary",
                role=role,
                champion_var=rv.champion,
                ban_cb=w.ban_cb,
                ban_var=rv.ban,
                delay_ms=0,
                allow_refresh=allow_refresh,
            )
            _schedule_ban_update(
                field_key=f"{role}:reserve1",
                role=role,
                champion_var=rv.reserve1_champion,
                ban_cb=w.reserve1_ban_cb,
                ban_var=rv.reserve1_ban,
                delay_ms=0,
                allow_refresh=allow_refresh,
            )
            _schedule_ban_update(
                field_key=f"{role}:reserve2",
                role=role,
                champion_var=rv.reserve2_champion,
                ban_cb=w.reserve2_ban_cb,
                ban_var=rv.reserve2_ban,
                delay_ms=0,
                allow_refresh=allow_refresh,
            )

    for role in ROLE_ORDER:
        rv = role_vars.get(role)
        w = role_widgets.get(role)
        if not rv or not w:
            continue

        rv.champion.trace_add(
            "write",
            lambda *_a, role=role, rv=rv, w=w: _schedule_ban_update(
                field_key=f"{role}:primary",
                role=role,
                champion_var=rv.champion,
                ban_cb=w.ban_cb,
                ban_var=rv.ban,
            ),
        )
        rv.reserve1_champion.trace_add(
            "write",
            lambda *_a, role=role, rv=rv, w=w: _schedule_ban_update(
                field_key=f"{role}:reserve1",
                role=role,
                champion_var=rv.reserve1_champion,
                ban_cb=w.reserve1_ban_cb,
                ban_var=rv.reserve1_ban,
            ),
        )
        rv.reserve2_champion.trace_add(
            "write",
            lambda *_a, role=role, rv=rv, w=w: _schedule_ban_update(
                field_key=f"{role}:reserve2",
                role=role,
                champion_var=rv.reserve2_champion,
                ban_cb=w.reserve2_ban_cb,
                ban_var=rv.reserve2_ban,
            ),
        )

    def _on_tab_changed(_event=None) -> None:
        try:
            tab_id = notebook.select()
            role = tab_to_role.get(str(tab_id) or "")
            if role:
                _apply_ranked_values_for_role(role)
        except Exception:
            pass

    notebook.bind("<<NotebookTabChanged>>", _on_tab_changed)

    def _canonicalize_or_error(
        role_label: str, field_label: str, raw_value: str, var: "tk.StringVar"
    ) -> Optional[str]:
        raw = str(raw_value or "").strip()
        v = _display_to_champion_name(raw)
        if not v:
            return ""
        if not champion_index:
            return v
        key = normalize_name(v)
        canonical = champion_index.get(key)
        if canonical is None:
            messagebox.showerror(
                "저장 실패",
                f"[{role_label}] {field_label} 값이 op.gg 챔피언 목록에 없습니다:\n{v}",
            )
            status_var.set("저장 실패: 챔피언명 오류")
            return None

        if canonical != v and raw == v:
            var.set(canonical)
        return canonical

    def _apply_save(close_after: bool) -> None:
        canon_by_role: Dict[str, Dict[str, str]] = {}
        for role in ROLE_ORDER:
            v = role_vars[role]
            role_label = ROLE_LABEL_KO.get(role, role)
            champ = _canonicalize_or_error(
                role_label, "champion", v.champion.get(), v.champion
            )
            if champ is None:
                return
            ban = _canonicalize_or_error(role_label, "ban", v.ban.get(), v.ban)
            if ban is None:
                return
            px = v.pick_x.get().strip()
            py = v.pick_y.get().strip()
            r1c = _canonicalize_or_error(
                role_label,
                "reserve1 champion",
                v.reserve1_champion.get(),
                v.reserve1_champion,
            )
            if r1c is None:
                return
            r1b = _canonicalize_or_error(
                role_label, "reserve1 ban", v.reserve1_ban.get(), v.reserve1_ban
            )
            if r1b is None:
                return
            r2c = _canonicalize_or_error(
                role_label,
                "reserve2 champion",
                v.reserve2_champion.get(),
                v.reserve2_champion,
            )
            if r2c is None:
                return
            r2b = _canonicalize_or_error(
                role_label, "reserve2 ban", v.reserve2_ban.get(), v.reserve2_ban
            )
            if r2b is None:
                return
            reserves_raw = [(r1c or "", r1b or ""), (r2c or "", r2b or "")]
            reserves = [(c, b) for (c, b) in reserves_raw if str(c or "").strip()]
            if not champ and (ban or px or py or reserves):
                messagebox.showerror(
                    "저장 실패",
                    f"[{role_label}] champion이 비어있습니다.\n"
                    "champion 없이 ban/pick_coord/reserve_picks를 저장하면 런타임 동작이 깨질 수 있습니다.",
                )
                status_var.set("저장 실패: 입력 검증 오류")
                return
            canon_by_role[role] = {
                "champion": str(champ or "").strip(),
                "ban": str(ban or "").strip(),
                "r1c": str(r1c or "").strip(),
                "r1b": str(r1b or "").strip(),
                "r2c": str(r2c or "").strip(),
                "r2b": str(r2b or "").strip(),
            }

        new_data: Dict[str, Dict] = (
            dict(config.data) if isinstance(config.data, dict) else {}
        )
        for role in ROLE_ORDER:
            v = role_vars[role]

            canon = canon_by_role.get(role, {})
            champ = str(canon.get("champion") or "").strip()
            if not champ:
                new_data.pop(role, None)
                continue

            entry = new_data.get(role) if isinstance(new_data.get(role), dict) else {}
            entry = dict(entry)

            entry["champion"] = champ

            ban = str(canon.get("ban") or "").strip()
            if ban:
                entry["ban"] = ban
            else:
                entry.pop("ban", None)

            coord = _parse_int_pair(v.pick_x.get(), v.pick_y.get())
            if coord is not None:
                entry["pick_coord"] = [int(coord[0]), int(coord[1])]
            else:
                entry.pop("pick_coord", None)

            reserves_raw = [
                (canon.get("r1c") or "", canon.get("r1b") or ""),
                (canon.get("r2c") or "", canon.get("r2b") or ""),
            ]
            reserves_norm = _normalize_reserves(champ, reserves_raw)
            if reserves_norm:
                entry["reserve_picks"] = reserves_norm
            else:
                entry.pop("reserve_picks", None)

            new_data[role] = entry

        try:
            config.data = new_data
            config.save()
        except Exception as exc:
            messagebox.showerror(
                "저장 실패", f"설정 파일 저장 중 오류가 발생했습니다:\n{exc}"
            )
            status_var.set("저장 실패: 파일 저장 오류")
            return

        status_var.set("저장 완료")
        if close_after:
            root.destroy()

    def _reload() -> None:
        try:
            fresh = ChampionConfig(path=config.path)
        except Exception as exc:
            messagebox.showerror(
                "불러오기 실패", f"설정 파일을 다시 불러오지 못했습니다:\n{exc}"
            )
            status_var.set("불러오기 실패")
            return

        config.data = fresh.data

        for role in ROLE_ORDER:
            info = config.get(role) or {}
            v = role_vars[role]
            v.champion.set(str(info.get("champion") or "").strip())
            v.ban.set(str(info.get("ban") or "").strip())
            coord = info.get("pick_coord") or None
            if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                v.pick_x.set(str(coord[0]))
                v.pick_y.set(str(coord[1]))
            else:
                v.pick_x.set("")
                v.pick_y.set("")
            reserves = config.get_reserve_picks(role)
            v.reserve1_champion.set(
                str(reserves[0][0]).strip() if len(reserves) >= 1 else ""
            )
            v.reserve1_ban.set(
                str(reserves[0][1]).strip() if len(reserves) >= 1 else ""
            )
            v.reserve2_champion.set(
                str(reserves[1][0]).strip() if len(reserves) >= 2 else ""
            )
            v.reserve2_ban.set(
                str(reserves[1][1]).strip() if len(reserves) >= 2 else ""
            )

        status_var.set("다시 불러옴")
        _refresh_configured_bans(allow_refresh=True)

    ttk.Button(btns, text="다시 불러오기", command=_reload).pack(side=tk.LEFT, padx=6)
    ttk.Button(btns, text="저장", command=lambda: _apply_save(close_after=False)).pack(
        side=tk.LEFT, padx=6
    )
    ttk.Button(
        btns, text="저장 후 닫기", command=lambda: _apply_save(close_after=True)
    ).pack(side=tk.LEFT, padx=6)
    ttk.Button(btns, text="닫기", command=root.destroy).pack(side=tk.LEFT, padx=6)

    _load_champion_list_async()

    root.mainloop()


if __name__ == "__main__":
    run_config_gui()

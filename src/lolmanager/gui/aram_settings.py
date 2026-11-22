from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Sequence

from lolmanager.core.aram import AramPreferences
from lolmanager.core.champion_names import normalize_name
from lolmanager.core.opgg_counter_recommendations import display_value_to_champion_name
from lolmanager.gui.champion_candidate_values import (
    build_champion_candidate_values,
    champion_rank_keys,
    champion_tier_display,
)


class AramSettingsPanel(ttk.Frame):
    def __init__(
        self,
        parent,
        *,
        preferences: AramPreferences,
        on_change: Callable[[], None],
    ) -> None:
        super().__init__(parent, padding=12)
        self.on_change = on_change
        self.name_index: dict[str, str] = {}
        self.champion_names: list[str] = []
        self.ranked_entries: list[tuple[str, str, str]] = []
        self.preferred_champions: list[str] = []
        self.manually_ranked: set[str] = set()
        self.enabled = tk.BooleanVar(self, value=preferences.enabled)
        self.champion = tk.StringVar(self)
        self.message = tk.StringVar(self)
        self.tier_status = tk.StringVar(self, value="칼바람 티어 불러오는 중…")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self.enable_check = ttk.Checkbutton(
            self, text="선호 챔피언 자동 선택", variable=self.enabled, command=on_change
        )
        self.enable_check.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            self,
            text=(
                "위쪽 챔피언을 더 선호합니다. 처음 제시된 카드 중 선호 챔피언을 자동으로 고르고,\n"
                "없으면 첫 번째 카드를 고릅니다. 직접 바꾸면 해당 픽창의 자동 선택을 멈춥니다."
            ),
            wraplength=640,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 12))

        add_row = ttk.Frame(self)
        add_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        add_row.columnconfigure(0, weight=1)
        self.champion_combo = ttk.Combobox(add_row, textvariable=self.champion)
        self.champion_combo.grid(row=0, column=0, sticky="ew")
        self.champion_combo.bind("<Return>", self._add)
        ttk.Button(add_row, text="추가", command=self._add).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(add_row, textvariable=self.tier_status).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        list_frame = ttk.Frame(self)
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.champions = tk.Listbox(list_frame, height=8, exportselection=False)
        self.champions.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.champions.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.champions.configure(yscrollcommand=scrollbar.set)
        buttons = ttk.Frame(self)
        buttons.grid(row=3, column=1, sticky="n", padx=(8, 0))
        ttk.Button(buttons, text="위로", command=lambda: self._move(-1)).pack(fill="x")
        ttk.Button(buttons, text="아래로", command=lambda: self._move(1)).pack(fill="x", pady=6)
        ttk.Button(buttons, text="삭제", command=self._remove).pack(fill="x")
        self.sort_button = ttk.Button(
            buttons, text="티어순 정렬", command=self._sort_by_tier, state=tk.DISABLED
        )
        self.sort_button.pack(fill="x", pady=(6, 0))
        ttk.Label(self, textvariable=self.message).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self.set_preferences(preferences)

    def set_champion_names(self, names: Sequence[str]) -> None:
        self.champion_names = list(names)
        self._refresh_candidates()

    def set_ranked_entries(
        self, entries: Sequence[tuple[str, str, str]], source: str
    ) -> None:
        self.ranked_entries = list(entries)
        self.sort_button.configure(state=tk.NORMAL if entries else tk.DISABLED)
        if entries:
            suffix = " · 저장된 자료, 갱신 실패" if source == "cache_stale" else ""
            self.tier_status.set(f"OP.GG 칼바람 · 티어/순위순{suffix}")
        else:
            self.tier_status.set("칼바람 티어를 불러오지 못해 이름순으로 표시합니다.")
        self._render_preferences(self.champions.curselection())

    def _refresh_candidates(self) -> None:
        names = [*self.champion_names, *(entry[0] for entry in self.ranked_entries)]
        self.name_index = {normalize_name(name): name for name in names}
        selected = {normalize_name(name) for name in self.preferred_champions}
        values = build_champion_candidate_values(
            ranked_entries=(e for e in self.ranked_entries if normalize_name(e[0]) not in selected),
            all_champion_values=[self.name_index[key] for key in sorted(self.name_index) if key not in selected],
        )
        self.champion_combo.configure(values=values)

    def _render_preferences(self, selection: tuple[int, ...] = ()) -> None:
        tiers = {normalize_name(name): tier for name, tier, _href in self.ranked_entries}
        self.champions.delete(0, tk.END)
        for name in self.preferred_champions:
            self.champions.insert(tk.END, champion_tier_display(name, tiers.get(normalize_name(name), "")))
        if selection:
            self.champions.selection_set(selection[0])
            self.champions.see(selection[0])
        self._refresh_candidates()

    def set_preferences(self, preferences: AramPreferences) -> None:
        self.enabled.set(preferences.enabled)
        self.preferred_champions = list(preferences.preferred_champions)
        self.manually_ranked = {normalize_name(name) for name in preferences.manually_ranked_champions}
        self._render_preferences()
        self.champion.set("")
        self.message.set("")

    def get_preferences(self) -> AramPreferences:
        return AramPreferences.from_config({
            "enabled": self.enabled.get(),
            "preferred_champions": self.preferred_champions,
            "manually_ranked_champions": [
                name for name in self.preferred_champions if normalize_name(name) in self.manually_ranked
            ],
        })

    def _add(self, _event=None) -> str:
        name = display_value_to_champion_name(self.champion.get())
        if not name:
            return "break"
        key = normalize_name(name)
        if self.name_index and key not in self.name_index:
            self.message.set("챔피언 이름을 확인하거나 목록에서 선택해 주세요.")
            return "break"
        if key in {normalize_name(value) for value in self.preferred_champions}:
            self.message.set("이미 선호 목록에 있는 챔피언입니다.")
            return "break"
        self.preferred_champions.append(self.name_index.get(key, name))
        self._render_preferences(self.champions.curselection())
        self.champion.set("")
        self.message.set("")
        self.on_change()
        return "break"

    def _move(self, delta: int) -> None:
        selected = self.champions.curselection()
        if not selected:
            return
        index = selected[0]
        target = index + delta
        if not 0 <= target < self.champions.size():
            return
        displaced = self.preferred_champions[target]
        name = self.preferred_champions.pop(index)
        self.preferred_champions.insert(target, name)
        self.manually_ranked.update((normalize_name(name), normalize_name(displaced)))
        self._render_preferences((target,))
        self.on_change()

    def _sort_by_tier(self) -> None:
        rank_keys = champion_rank_keys(self.ranked_entries)
        if not rank_keys:
            return
        sortable = [
            name for name in self.preferred_champions if normalize_name(name) not in self.manually_ranked
        ]
        ordered = iter(sorted(sortable, key=lambda name: rank_keys.get(normalize_name(name), (99, 0))))
        result = [
            name if normalize_name(name) in self.manually_ranked else next(ordered)
            for name in self.preferred_champions
        ]
        self.message.set("티어순 정렬 완료 · 직접 옮긴 위치 유지")
        if result != self.preferred_champions:
            self.preferred_champions = result
            self._render_preferences()
            self.on_change()

    def _remove(self) -> None:
        selected = self.champions.curselection()
        if selected:
            name = self.preferred_champions.pop(selected[0])
            self.manually_ranked.discard(normalize_name(name))
            self._render_preferences()
            self.on_change()

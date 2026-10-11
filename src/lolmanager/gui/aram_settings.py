from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Sequence

from lolmanager.core.aram import AramPreferences
from lolmanager.core.champion_names import normalize_name


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
        self.enabled = tk.BooleanVar(self, value=preferences.enabled)
        self.champion = tk.StringVar(self)
        self.message = tk.StringVar(self)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self.enable_check = ttk.Checkbutton(
            self, text="선호 챔피언 자동 선택", variable=self.enabled, command=on_change
        )
        self.enable_check.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            self,
            text=(
                "위쪽 챔피언을 더 선호합니다. 현재 챔피언보다 우선순위가 높은 챔피언이\n"
                "교환 대기 목록(벤치)에 나타나면 자동으로 선택합니다. 없으면 현재 챔피언을 유지합니다."
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
        ttk.Label(self, textvariable=self.message).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self.set_preferences(preferences)

    def set_champion_names(self, names: Sequence[str]) -> None:
        self.name_index = {normalize_name(name): name for name in names}
        self.champion_combo.configure(values=list(names))

    def set_preferences(self, preferences: AramPreferences) -> None:
        self.enabled.set(preferences.enabled)
        self.champions.delete(0, tk.END)
        for name in preferences.preferred_champions:
            self.champions.insert(tk.END, name)
        self.champion.set("")
        self.message.set("")

    def get_preferences(self) -> AramPreferences:
        return AramPreferences.from_config({
            "enabled": self.enabled.get(),
            "preferred_champions": list(self.champions.get(0, tk.END)),
        })

    def _add(self, _event=None) -> str:
        name = " ".join(self.champion.get().split())
        if not name:
            return "break"
        key = normalize_name(name)
        if self.name_index and key not in self.name_index:
            self.message.set("챔피언 이름을 확인하거나 목록에서 선택해 주세요.")
            return "break"
        if key in {normalize_name(value) for value in self.champions.get(0, tk.END)}:
            self.message.set("이미 선호 목록에 있는 챔피언입니다.")
            return "break"
        self.champions.insert(tk.END, self.name_index.get(key, name))
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
        name = self.champions.get(index)
        self.champions.delete(index)
        self.champions.insert(target, name)
        self.champions.selection_set(target)
        self.champions.see(target)
        self.on_change()

    def _remove(self) -> None:
        selected = self.champions.curselection()
        if selected:
            self.champions.delete(selected[0])
            self.on_change()

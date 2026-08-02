"""services/repositories/staff_repository.py — the technician roster."""

from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseRepository

# Roles that can actually be handed a vehicle.
DISPATCHABLE_ROLES = ("technician", "general_manager", "photographer")


class StaffRepository(BaseRepository):

    def get(self, studio_id: int, staff_id: int) -> Dict[str, Any]:
        return self.fetch_one("SELECT * FROM staff WHERE id=? AND studio_id=?", (staff_id, studio_id))

    def find_by_name(self, studio_id: int, name: str) -> Dict[str, Any]:
        return self.fetch_one("SELECT * FROM staff WHERE studio_id=? AND name=?", (studio_id, name))

    def list_all(self, studio_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all("SELECT * FROM staff WHERE studio_id=? ORDER BY name", (studio_id,))

    def list_dispatchable(self, studio_id: int) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in DISPATCHABLE_ROLES)
        return self.fetch_all(
            f"SELECT id, name, role, username FROM staff WHERE studio_id=? "
            f"AND role IN ({placeholders}) ORDER BY CASE role WHEN 'technician' THEN 0 ELSE 1 END, name",
            (studio_id, *DISPATCHABLE_ROLES),
        )

    def technicians(self, studio_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT id, name, role FROM staff WHERE studio_id=? AND role='technician' ORDER BY name",
            (studio_id,),
        )

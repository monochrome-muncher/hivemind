// Hivemind admin panel: what the fleet page works out of the lists it reads. Pure: no DOM.

/** The merges and moves that took `fleetId` as source or target. */
export const recordsForFleet = (records, fleetId) =>
  records.filter((r) => r.source_fleet_id === fleetId || r.target_fleet_id === fleetId);

/** The agents whose home fleet is `fleetId`, by name. */
export const membersOf = (agents, fleetId) =>
  agents.filter((a) => a.home_fleet_id === fleetId).sort((a, b) => a.name.localeCompare(b.name));

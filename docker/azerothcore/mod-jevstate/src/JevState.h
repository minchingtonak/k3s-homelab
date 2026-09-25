/*
 * mod-jevstate — read-only combat state endpoint for an out-of-client agent.
 *
 * Exposes a tiny HTTP server inside the worldserver serving per-player combat
 * snapshots as JSON, so a game loop on the desktop can perceive exact game
 * state (server ground truth: cooldowns, cast bars, threat, attackers)
 * without OCR, memory reading, or any client modification.
 *
 * Read-only by design: the module never accepts commands and never mutates
 * the world. Acting on the state stays client-side (input emulation in the
 * agent's game loop).
 *
 * Threading model:
 *   - WorldScript::OnUpdate runs on the world thread and periodically
 *     serializes every online player's snapshot into a mutex-guarded cache.
 *     All Player/Unit API access happens here, where it is safe.
 *   - The HTTP accept thread only ever serves cached strings. It touches no
 *     game objects, so it needs no map locks.
 *
 * Endpoints:
 *   GET /health          -> {"ok":true,...}
 *   GET /state/<Name>    -> latest snapshot for that character (cached at
 *                           JevState.UpdateMs cadence; case-insensitive name)
 *   GET /nav/path/<Name>?x=&y=&z=[&force=1]
 *                        -> mmaps navmesh path from that player's CURRENT
 *                           position to (x,y,z). The HTTP thread enqueues the
 *                           request; the world thread computes it with
 *                           PathGenerator (the same API creatures use) on
 *                           the next world tick and hands back JSON:
 *                           {status, length_yd, points[[x,y,z],...], elapsed_ms}.
 *                           status: normal|shortcut|incomplete|nopath|not_using_path|
 *                           short|farfrompoly|blank|error|timeout.
 *                           Still read-only: it never moves anyone.
 *
 * Config (conf/JevState.conf.dist, overridable via AC_JEVSTATE_* env):
 *   JevState.Enable, JevState.BindIP, JevState.Port, JevState.UpdateMs,
 *   JevState.MaxAttackers, JevState.AttackerRangeYards
 */

#ifndef MOD_JEVSTATE_H
#define MOD_JEVSTATE_H

#include "Define.h"
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

class JevStateWorldScript;

// One /nav/path request in flight (HTTP thread -> world thread).
struct JevNavRequest
{
    uint64_t id = 0;
    std::string name;      // player name, as given in the URL
    float x = 0.f, y = 0.f, z = 0.f;
    bool forceDest = false;
};

class JevState
{
public:
    static JevState& Instance();

    void LoadConfig();                 // (re)read sConfigMgr options
    uint32_t UpdateMs() const { return _updateMs; }
    bool StartHttp();                  // spawn accept thread (world ready)
    void StopHttp();                   // idempotent shutdown

    void UpdateSnapshots(uint32_t nowMs); // world thread only

    // Nav path plumbing. RequestNavPath runs on the HTTP thread: it enqueues
    // and blocks (bounded) until the world thread answers. ProcessNavRequests
    // runs on the world thread every tick and computes pending paths with
    // PathGenerator, where map/navmesh context is valid.
    void ProcessNavRequests();                              // world thread only
    std::string RequestNavPath(std::string const& name,     // HTTP thread only
                               float x, float y, float z, bool forceDest);

    // HTTP thread only: latest cached snapshot ("" if none yet)
    std::string GetSnapshot(std::string const& name) const;
    std::string HealthJson() const;

private:
    JevState() = default;
    void AcceptLoop();

    // config
    bool _enabled = true;
    std::string _bindIp = "0.0.0.0";
    uint16_t _port = 7879;
    uint32_t _updateMs = 100;
    uint32_t _maxAttackers = 8;
    float _attackerRangeYards = 45.0f;

    // snapshot cache: lowercase char name -> serialized JSON
    mutable std::mutex _cacheMutex;
    std::map<std::string, std::string> _cache;
    uint64_t _frame = 0;               // monotonic snapshot counter
    std::atomic<uint64_t> _lastUpdateMs{0};

    // nav path queue: HTTP thread produces, world thread consumes
    std::mutex _navMutex;
    std::condition_variable _navCv;
    std::vector<JevNavRequest> _navPending;
    std::map<uint64_t, std::pair<std::string, int64_t>> _navResults; // id -> (json, expiryMs)
    uint64_t _navNextId = 1;

    // http
    std::thread _httpThread;
    std::atomic<bool> _running{false};
    bool _threadStarted = false;
    int _listenFd = -1;
};

#define sJevState JevState::Instance()

#endif // MOD_JEVSTATE_H

#pragma once

namespace datom {

class Engine;

enum class SessionPhase { Startup, Ready, AwaitSync };

// Consume one frontend message. A false return ends the session; the caller owns fd.
bool serve_message(int fd, Engine& engine, SessionPhase& phase);

} // namespace datom

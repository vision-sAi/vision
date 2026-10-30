import Foundation
import UIKit

enum Role: String {
    case user, assistant, note
    case shell   // a command run in shell mode (`!`): `text` is the command
}

/// One entry of the transcript on screen.
struct Message: Identifiable, Equatable {
    let id: UUID
    var role: Role
    var text: String
    var status: String = ""      // "using Bash…" / "thinking…" while a reply streams
    var streaming = false
    var isError = false
    var images: [UIImage] = []   // photos sent with a user turn, shown above its bubble
    var imagePaths: [String] = []   // where the machine keeps those photos; refetched when `images` is empty
    var files: [String] = []        // other files sent with a user turn (their machine paths), shown as chips
    var attached: [String] = []     // photos and files together, in the order they were sent: the machine matches a queued message on it
    var answered: [AnsweredForm] = []   // question forms settled mid-reply, drawn where the reply had got to
    var agents: [AgentRow] = []          // subagents the reply spawned (Agent calls, an ultracode workflow's agents)
    var tools: [ToolRow] = []            // the reply's own tool calls (Bash, Read, …), drawn where they ran
    var startedAt: Date?                 // when the turn this reply belongs to began: its timer counts from here while it streams
    var pausedAt: Date?                  // a question has been waiting on the user since: the timer holds still until it is answered
    var durationMs: Int?                 // how long the turn took, once it is done (the footer under the reply)
    var queued = false                   // sent while a reply was running: waits on the machine until that reply ends
    var queueSeen = false                // …and the machine has listed it in its queue (until then it's on its way)
    var shellOutput = ""                 // a shell row: what the command printed (the end of it)
    var shellCode: Int?                  // …its exit code; nil while it runs, or when it was stopped
    var shellRunning = false
    var checkin: CheckinCard?            // a scout's check-in (role note): drawn as a card with `text` as its report

    init(id: UUID = UUID(), role: Role, text: String, status: String = "", streaming: Bool = false, isError: Bool = false,
         images: [UIImage] = [], imagePaths: [String] = [], files: [String] = []) {
        self.id = id
        self.role = role
        self.text = text
        self.status = status
        self.streaming = streaming
        self.isError = isError
        self.images = images
        self.imagePaths = imagePaths
        self.files = files
    }

    /// A turn as the machine reports it: the photo note it appends to a user message is split back out.
    static func fromServer(role: Role, text: String) -> Message {
        let (clean, paths, files, order) = splitAttachments(text)
        var message = Message(role: role, text: clean, imagePaths: paths, files: files)
        message.attached = order
        return message
    }

    /// Same user turn as another, ignoring the photo note and surrounding whitespace.
    func sameTurn(as other: Message) -> Bool {
        role == other.role && trimmedText == other.trimmedText && imagePaths == other.imagePaths && files == other.files
    }

    var trimmedText: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// Whether the i-th thumbnail stands for a video (the machine keeps it under a video extension).
    func isVideo(at i: Int) -> Bool {
        guard imagePaths.indices.contains(i) else { return false }
        return ["mp4", "mov", "m4v"].contains((imagePaths[i] as NSString).pathExtension.lowercased())
    }

    /// Photos and videos go up as themselves; anything else from the Files picker is a file.
    static func isMedia(_ path: String) -> Bool {
        ["jpg", "jpeg", "png", "heic", "webp", "gif", "mp4", "mov", "m4v"].contains((path as NSString).pathExtension.lowercased())
    }

    /// How long a reply took, the way the terminal's footer says it: "12.3s".
    static func took(_ seconds: TimeInterval) -> String {
        String(format: "%.1fs", max(0, seconds))
    }

    /// A file's own name: the machine saves it as "<date>-<time>-<hex>-<name>".
    static func fileName(_ path: String) -> String {
        let name = (path as NSString).lastPathComponent
        let parts = name.split(separator: "-", maxSplits: 3, omittingEmptySubsequences: false)
        return parts.count == 4 ? String(parts[3]) : name
    }

    /// `vision serve` sends the brain "[Attached image: path]" / "[Attached video: path]" / "[Attached file: path]"
    /// lines (a video followed by its frames and transcript, and nudges when there was no text);
    /// history and busy-state text come back in that form.
    static func splitAttachments(_ text: String) -> (text: String, paths: [String], files: [String], order: [String]) {
        var paths: [String] = []
        var files: [String] = []
        var order: [String] = []
        var kept: [String] = []
        let nudges = ["Look at the attached image(s).", "Watch the attached video(s) through their frames.", "Read the attached file(s)."]
        for line in text.components(separatedBy: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("[Attached file: "), t.hasSuffix("]") {
                files.append(String(t.dropFirst("[Attached file: ".count).dropLast()))
                order.append(files[files.count - 1])
            } else if t.hasPrefix("[Attached image: "), t.hasSuffix("]") {
                paths.append(String(t.dropFirst("[Attached image: ".count).dropLast()))
                order.append(paths[paths.count - 1])
            } else if t.hasPrefix("[Attached video: "), t.hasSuffix("]") {
                paths.append(String(t.dropFirst("[Attached video: ".count).dropLast()))
                order.append(paths[paths.count - 1])
            } else if !paths.isEmpty && t.hasSuffix("]") && ["[Video length ", "[Frame ", "[Video audio, transcribed: "].contains(where: t.hasPrefix) {
                continue  // a video's frame list and transcript, for the brain only
            } else if nudges.contains(t) && !(paths.isEmpty && files.isEmpty) {
                continue
            } else {
                kept.append(line)
            }
        }
        guard !(paths.isEmpty && files.isEmpty) else { return (text, [], [], []) }
        return (kept.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines), paths, files, order)
    }
}

/// An AskUserQuestion form as it was settled, pinned to how far the reply had got (`at`, in characters).
struct AnsweredForm: Hashable {
    struct Row: Hashable {
        let question: String
        let answer: String   // the options on offer when the form was declined
    }
    let at: Int
    let declined: Bool
    let rows: [Row]

    /// From an `answered` frame: {"questions": [...], "answers": {question: answer} or null}.
    init?(_ ev: [String: Any], at: Int) {
        let questions = ev["questions"] as? [[String: Any]] ?? []
        let answers = ev["answers"] as? [String: String] ?? [:]
        self.at = at
        declined = answers.isEmpty
        if declined {
            rows = questions.map { q in
                let labels = (q["options"] as? [[String: Any]] ?? []).compactMap { $0["label"] as? String }
                return Row(question: q["question"] as? String ?? "", answer: labels.joined(separator: " / "))
            }
        } else {
            // In the form's order, not the dictionary's.
            let order = questions.compactMap { $0["question"] as? String }
            let keys = order.filter { answers[$0] != nil } + answers.keys.filter { !order.contains($0) }.sorted()
            rows = keys.map { Row(question: $0, answer: answers[$0] ?? "") }
        }
        if rows.isEmpty { return nil }
    }
}

/// A subagent's row, rebuilt from the machine's `agent` frames: an Agent call, or one agent of an
/// ultracode workflow (kind = its phase, label = its name).
struct AgentStep: Hashable, Decodable {
    var tool: String
    var detail: String
}

struct AgentRow: Identifiable, Hashable {
    let id: String
    let at: Int              // how much of the reply had streamed when it started: where its block sits
    var started: Date        // the machine's own start time when the frame has one, so a late joiner times it right
    var tokens = 0
    var kind = "agent"
    var label = ""
    var model = ""
    var effort = ""
    var tools = 0
    var step = ""            // its newest tool call, "Read vision/brain.py"
    var steps: [AgentStep] = []
    var prompt = ""
    var summary = ""
    var details: [String] = []
    var done = false
    var failed = false
    var cutOff = false
    var status = ""          // "3 tools · 17.3s" once done

    init(id: String, at: Int, started: Date = .now) {
        self.id = id
        self.at = at
        self.started = started
    }

    /// Fold one `agent` frame in.
    mutating func apply(_ ev: [String: Any]) {
        if let v = ev["started"] as? Double, v > 0 { started = Date(timeIntervalSince1970: v) }
        if let v = ev["tokens"] as? Int, v > 0 { tokens = v }
        if let v = ev["kind"] as? String, !v.isEmpty { kind = v }
        if let v = ev["label"] as? String, !v.isEmpty { label = v }
        if let v = ev["model"] as? String, !v.isEmpty { model = v }
        if let v = ev["effort"] as? String, !v.isEmpty { effort = v }
        if let v = ev["tools"] as? Int { tools = max(tools, v) }
        if let v = ev["prompt"] as? String { prompt = v }
        if let v = ev["summary"] as? String { summary = v }
        if let v = ev["details"] as? [String] { details = v }
        if let all = ev["steps"] as? [[String: Any]] {
            steps = all.map { AgentStep(tool: $0["tool"] as? String ?? "", detail: $0["detail"] as? String ?? "") }
        } else if let s = ev["step"] as? [String: Any] {
            let next = AgentStep(tool: s["tool"] as? String ?? "", detail: s["detail"] as? String ?? "")
            if steps.last != next { steps.append(next) }
        }
        if let s = ev["step"] as? [String: Any] {
            step = [s["tool"] as? String ?? "", s["detail"] as? String ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
        }
        if ev["done"] as? Bool ?? false {
            done = true
            failed = ev["failed"] as? Bool ?? false
            cutOff = ev["cut_off"] as? Bool ?? false
            status = ev["status"] as? String ?? status
        }
    }
}

/// One field a tool was called with, as the machine sends it (`["command", "git status"]`), its key
/// spelt for the page: `file_path` → `File path`.
struct ToolInput: Hashable {
    let key: String
    let value: String

    init?(_ pair: [String]) {
        guard pair.count == 2 else { return nil }
        key = pair[0]
        value = pair[1]
    }

    var label: String {
        let words = key.replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "([a-z])([A-Z])", with: "$1 $2", options: .regularExpression)
            .lowercased()
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    /// Code, drawn monospaced: a command, a pattern, a script.
    var isCode: Bool { ["command", "pattern", "script", "code", "cmd", "arguments", "glob"].contains(key) }
}

/// One hunk of an edit: where it starts in the old and new file (0 when unknown) and its lines, each
/// led by " ", "-" or "+".
struct DiffHunk: Hashable, Decodable {
    let old: Int
    let new: Int
    let lines: [String]
}

struct DiffLine: Hashable {
    let number: Int?
    let sign: Character
    let text: String
}

/// One tool call of the reply itself, rebuilt from the machine's `tool` frames: `Bash  git status`, and
/// what came back (shown on a tap).
struct ToolRow: Identifiable, Hashable {
    let id: String
    let at: Int              // how much of the reply had streamed when it started: where its block sits
    var name = ""
    var detail = ""
    var done = false
    var isError = false
    var output = ""
    var diff: [DiffHunk] = []    // an edit's changes, drawn as Claude Code does
    var input: [ToolInput] = []  // what it was called with, in full, for its page in the calls sheet

    init(id: String, at: Int) {
        self.id = id
        self.at = at
    }

    /// Fold one `tool` frame in.
    mutating func apply(_ ev: [String: Any]) {
        if let v = ev["name"] as? String, !v.isEmpty { name = v }
        if let v = ev["detail"] as? String, !v.isEmpty { detail = v }
        done = ev["done"] as? Bool ?? done
        isError = ev["is_error"] as? Bool ?? isError
        if let v = ev["output"] as? String { output = v }
        if let v = ev["diff"] as? [[String: Any]] {
            diff = v.map { DiffHunk(old: $0["old"] as? Int ?? 0, new: $0["new"] as? Int ?? 0, lines: $0["lines"] as? [String] ?? []) }
        }
        if let v = ev["input"] as? [[String]] { input = v.compactMap(ToolInput.init) }
    }

    /// A call that changes a file: a block of its own with its diff, not a line in the run.
    var isEdit: Bool { ["Edit", "MultiEdit", "Write", "NotebookEdit"].contains(name) }

    /// The diff as numbered lines, a `nil` line between hunks.
    var diffLines: [DiffLine?] {
        var out: [DiffLine?] = []
        for (i, hunk) in diff.enumerated() {
            if i > 0 { out.append(nil) }
            var old = hunk.old, new = hunk.new
            for raw in hunk.lines {
                let sign = raw.first ?? " "
                let text = String(raw.dropFirst()).replacingOccurrences(of: "\t", with: "    ")
                let number = sign == "-" ? old : new
                out.append(DiffLine(number: number > 0 ? number : nil, sign: sign, text: text))
                if sign != "+" { old += 1 }
                if sign != "-" { new += 1 }
            }
        }
        return out
    }

    var additions: Int { diff.reduce(0) { $0 + $1.lines.filter { $0.hasPrefix("+") }.count } }
    var removals: Int { diff.reduce(0) { $0 + $1.lines.filter { $0.hasPrefix("-") }.count } }

    /// Calls in runs with no edit between them, and each edit on its own, in order.
    static func groups(_ rows: [ToolRow]) -> [[ToolRow]] {
        var groups: [[ToolRow]] = []
        for row in rows {
            if row.isEdit || groups.last?.first?.isEdit != false { groups.append([row]) }
            else { groups[groups.count - 1].append(row) }
        }
        return groups
    }

    /// The SF Symbol for the kind of call.
    var symbol: String {
        switch name {
        case "Read", "view_image": "doc.text"
        case "Edit", "Write", "NotebookEdit", "File change": "pencil"
        case "Bash", "Command", "shell": "terminal"
        case "Grep", "Glob": "magnifyingglass"
        case "WebSearch", "Web search", "web_search": "globe"
        case "WebFetch": "safari"
        case "Agent", "Task": "person.2"
        case "ToolSearch", "Skill": "wrench.and.screwdriver"
        default: "gearshape"
        }
    }

    /// What it is doing, in words: `Reading`, `Running`, … (the tool's own name for anything else).
    var doing: String { Self.verbs[name]?.0 ?? (name.isEmpty ? "Working" : name) }
    /// …and once done: `Read`, `Ran`, ….
    var did: String { Self.verbs[name]?.1 ?? (name.isEmpty ? "Tool" : name) }

    private static let verbs: [String: (String, String)] = [
        "Read": ("Reading", "Read"), "Edit": ("Editing", "Edited"), "Write": ("Writing", "Wrote"),
        "NotebookEdit": ("Editing", "Edited"), "Bash": ("Running", "Ran"), "Grep": ("Searching for", "Searched for"),
        "Glob": ("Looking for", "Looked for"), "WebSearch": ("Searching the web for", "Searched the web for"),
        "WebFetch": ("Reading", "Read"), "Agent": ("Handing off", "Handed off"), "Task": ("Handing off", "Handed off"),
        "ToolSearch": ("Loading tools", "Loaded tools"),
        "ExitPlanMode": ("Waiting for your go-ahead", "Proposed a plan"),
        "AskUserQuestion": ("Waiting for your answer", "Asked you"),
    ]

    /// A call that is only waiting on the user (a plan approval, a question form): nothing is running.
    var waitsOnUser: Bool { !done && (name == "ExitPlanMode" || name == "AskUserQuestion") }

    /// The live line: `Reading ChatView.swift`, `Running git status`. A command that came with its own description
    /// ("Run the full test suite") already starts with a verb, so that verb takes the -ing instead of doubling up.
    var live: String {
        let what = subject
        if name == "Bash", what.first?.isUppercase == true {
            let space = what.firstIndex { !$0.isLetter } ?? what.endIndex
            return Self.gerund(String(what[..<space])) + what[space...]
        }
        return what.isEmpty ? doing : "\(doing) \(what)"
    }

    /// `Run` → `Running`, `Make` → `Making`, `List` → `Listing`, keeping the capital.
    private static func gerund(_ verb: String) -> String {
        let w = verb.lowercased()
        let irregular = ["be": "being", "see": "seeing", "free": "freeing", "die": "dying", "lie": "lying", "tie": "tying"]
        var out: String
        if let hit = irregular[w] {
            out = hit
        } else if w.hasSuffix("ing") {
            return verb
        } else if w.hasSuffix("ie") {
            out = String(w.dropLast(2)) + "ying"
        } else if w.hasSuffix("e"), !w.hasSuffix("ee"), !w.hasSuffix("ye"), !w.hasSuffix("oe") {
            out = String(w.dropLast()) + "ing"
        } else if ["run", "stop", "get", "set", "put", "cut", "hit", "let", "sit", "shut", "grep", "drop", "plan", "ship",
                   "skip", "tap", "swap", "strip", "trim", "wrap", "zip", "log", "tag", "chop", "dig", "pin", "spin",
                   "begin", "commit", "rerun", "reset", "submit", "admit", "permit", "prefer", "refer", "omit"].contains(w) {
            out = w + String(w.last!) + "ing"
        } else {
            out = w + "ing"
        }
        return out.prefix(1).uppercased() + out.dropFirst()
    }

    /// What it is working on, short: a file's name rather than its path, a page's host, a command's first line.
    var subject: String {
        switch name {
        case "Read", "Edit", "Write", "NotebookEdit":
            return (detail as NSString).lastPathComponent
        case "WebFetch":
            return URL(string: detail)?.host() ?? detail
        case "ToolSearch", "ExitPlanMode", "AskUserQuestion":
            return ""
        default:
            return String(detail.split(separator: "\n", maxSplits: 1).first ?? "")
        }
    }

    /// A finished run of calls in one line, as the terminal folds them: `Read 3 files, ran 2 commands`.
    static func summary(_ rows: [ToolRow]) -> String {
        let verbs: [String: (String, String)] = [
            "Read": ("read", "file"), "Edit": ("edited", "file"), "Write": ("edited", "file"), "NotebookEdit": ("edited", "file"),
            "Bash": ("ran", "command"), "Grep": ("searched", "pattern"), "Glob": ("searched", "pattern"),
            "WebSearch": ("searched the web", "time"), "WebFetch": ("fetched", "page"), "Agent": ("ran", "agent"), "Task": ("ran", "agent"),
        ]
        var order: [String] = []
        var counts: [String: (verb: String, noun: String, n: Int)] = [:]
        for row in rows {
            let (verb, noun) = verbs[row.name] ?? ("called", row.name)
            let key = verb + "|" + noun
            if counts[key] == nil { order.append(key); counts[key] = (verb, noun, 0) }
            counts[key]!.n += 1
        }
        var parts = order.compactMap { counts[$0] }.map { c in
            c.verb == "called" ? "called \(c.noun) \(c.n) time\(c.n == 1 ? "" : "s")" : "\(c.verb) \(c.n) \(c.noun)\(c.n == 1 ? "" : "s")"
        }
        let failed = rows.filter(\.isError).count
        if failed > 0 { parts.append("\(failed) failed") }
        let line = parts.joined(separator: ", ")
        return line.prefix(1).uppercased() + line.dropFirst()
    }
}

/// A piece of an assistant reply: prose, a picture it shows, a question form it asked, the subagents it ran, or its tool calls.
enum ReplyPart: Hashable {
    case text(String)
    case image(path: String, caption: String)
    case answered(AnsweredForm)
    case agents([AgentRow])
    case tools([ToolRow])
    case plan(String)

    var isTools: Bool {
        if case .tools = self { true } else { false }
    }

    var hasRunningAgent: Bool {
        if case .agents(let rows) = self { rows.contains { !$0.done } } else { false }
    }

    /// Something the reply said or showed, as opposed to a block of calls or agents.
    var isProse: Bool {
        switch self {
        case .text, .image, .answered, .plan: true
        case .agents, .tools: false
        }
    }
}

extension Message {
    /// `![caption](/abs/path.png)` (or `~/…`, `file:///…`): a picture on the machine the reply wants shown.
    private static let replyImage = try! NSRegularExpression(
        pattern: #"!\[([^\]\n]*)\]\(\s*<?(?:file://)?((?:/|~/)[^)>\n]+?\.(?:png|jpe?g|gif|webp|heic))>?\s*\)"#,
        options: [.caseInsensitive])

    /// The reply split into prose, pictures and settled question forms, in order. While it streams, a picture still being
    /// written (`![…` with no closing bracket yet) is held back rather than shown as raw markdown.
    var replyParts: [ReplyPart] {
        var source = text
        if streaming, let open = source.range(of: "![", options: .backwards),
           source[open.lowerBound...].firstIndex(of: ")") == nil {
            source = String(source[..<open.lowerBound])
        }
        // Agents that started at the same point of the reply (a batch of Agent calls, a workflow) share a block.
        var groups: [[AgentRow]] = []
        for agent in agents {
            if let last = groups.last?.first, last.at == agent.at { groups[groups.count - 1].append(agent) } else { groups.append([agent]) }
        }
        // …and so do tool calls with no prose between them.
        var runs: [[ToolRow]] = []
        for tool in tools {
            if let last = runs.last?.first, last.at == tool.at { runs[runs.count - 1].append(tool) } else { runs.append([tool]) }
        }
        let inserts: [(at: Int, part: ReplyPart)] = answered.map { ($0.at, .answered($0)) } + groups.map { ($0[0].at, .agents($0)) }
            + runs.map { ($0[0].at, .tools($0)) }
        guard !inserts.isEmpty else { return Self.parts(of: source) }
        // Split around the answered forms and agent blocks, the way the terminal breaks the reply at each one.
        var parts: [ReplyPart] = []
        var start = source.startIndex
        for insert in inserts.enumerated().sorted(by: { ($0.element.at, $0.offset) < ($1.element.at, $1.offset) }).map(\.element) {
            let scalars = source.unicodeScalars
            let end = scalars.index(scalars.startIndex, offsetBy: min(max(0, insert.at), scalars.count))
            if end > start { parts += Self.parts(of: String(source[start..<end])) }
            parts.append(insert.part)
            start = max(start, end)
        }
        return Self.liftingPlans(parts + Self.parts(of: String(source[start...])))
    }

    /// A plan the reply proposed (ExitPlanMode) arrives as prose straight after its call: that prose becomes a plan
    /// card and the call's own row goes, since the card says it. While the plan is still flowing out, the part shown
    /// so far is the card.
    private static func liftingPlans(_ parts: [ReplyPart]) -> [ReplyPart] {
        var out: [ReplyPart] = []
        var i = 0
        while i < parts.count {
            defer { i += 1 }
            guard case .tools(let rows) = parts[i],
                  let call = rows.last(where: { $0.name == "ExitPlanMode" }),
                  let plan = call.input.first(where: { $0.key == "plan" })?.value.trimmingCharacters(in: .whitespacesAndNewlines),
                  !plan.isEmpty, i + 1 < parts.count, case .text(let next) = parts[i + 1] else {
                out.append(parts[i])
                continue
            }
            let text = next.trimmingCharacters(in: .whitespacesAndNewlines)
            guard text.hasPrefix(plan) || plan.hasPrefix(text) else {
                out.append(parts[i])
                continue
            }
            let others = rows.filter { $0.id != call.id }
            if !others.isEmpty { out.append(.tools(others)) }
            out.append(.plan(String(text.prefix(plan.count))))
            let rest = text.dropFirst(plan.count).trimmingCharacters(in: .whitespacesAndNewlines)
            if !rest.isEmpty { out.append(.text(rest)) }
            i += 1
        }
        return out
    }

    private static func parts(of source: String) -> [ReplyPart] {
        let ns = source as NSString
        var parts: [ReplyPart] = []
        var cursor = 0
        func prose(upTo end: Int) {
            let chunk = ns.substring(with: NSRange(location: cursor, length: end - cursor)).trimmingCharacters(in: .newlines)
            if !chunk.trimmingCharacters(in: .whitespaces).isEmpty { parts.append(.text(chunk)) }
        }
        for match in replyImage.matches(in: source, range: NSRange(location: 0, length: ns.length)) {
            prose(upTo: match.range.location)
            parts.append(.image(path: ns.substring(with: match.range(at: 2)),
                                caption: ns.substring(with: match.range(at: 1)).trimmingCharacters(in: .whitespaces)))
            cursor = match.range.location + match.range.length
        }
        prose(upTo: ns.length)
        return parts
    }
}

/// An AskUserQuestion form the machine wants answered (same shape Claude Code sends).
struct Question: Decodable, Identifiable, Equatable {
    struct Option: Decodable, Identifiable, Equatable {
        var id: String { label }
        let label: String
        let description: String?
    }

    var id: String { question }
    let question: String
    let header: String?
    let multiSelect: Bool?
    let options: [Option]
}

struct QuestionForm: Identifiable {
    let id = UUID()
    let questions: [Question]
}

/// One chat as the machine reports it (the `chat` frame / an entry of `hello.chats`).
struct ChatInfo: Identifiable, Equatable {
    let id: String
    var source: String         // "server": runs inside `vision serve`; "terminal": a `vision` chat open in a terminal
    var title: String
    var provider: String
    var model: String
    var modelID: String
    var effort: String
    var mode = ""              // "auto" or "plan"; "" from a Vision too old to say (the mode button stays away)
    var folder = ""            // where the chat works on the machine, home written as ~ ("" from an older Vision)
    var sessionID: String?
    var contextTokens: Int?    // what the model read on its last call, and its window: the composer's gauge
    var contextWindow: Int?
    var historyID: String
    var busy: Bool
    var waiting: Bool          // an AskUserQuestion form is open on the machine
    var partial: String
    var elapsedMs: Int = 0     // how long the running reply has taken so far (0 when idle, or from an older Vision)
    var userText: String
    var queued: [String]       // messages waiting behind the reply on the machine, oldest first
    var shellRunning = false   // a shell-mode command is running in this chat's folder: the stop button stops it
    var suggestion = ""        // the message the machine guesses comes next (vision/suggest.py): the empty composer's grey text
    var project = ""           // the project this is the chat of ("" for an ordinary chat, or from an older Vision)
    var pinned = false         // pinned to the top of the list, the same on every app (the machine keeps it)
    var created: Double
    var updated: Double

    var isTerminal: Bool { source == "terminal" }

    init?(_ ev: [String: Any]) {
        guard let id = ev["chat"] as? String else { return nil }
        self.id = id
        source = ev["source"] as? String ?? "server"
        title = ev["title"] as? String ?? ""
        provider = ev["provider"] as? String ?? ""
        model = ev["model"] as? String ?? ""
        modelID = ev["model_id"] as? String ?? ""
        effort = ev["effort"] as? String ?? ""
        mode = ev["mode"] as? String ?? ""
        folder = ev["folder"] as? String ?? ""
        sessionID = ev["session_id"] as? String
        if let ctx = ev["context"] as? [String: Any], let tokens = ctx["tokens"] as? Int, let window = ctx["window"] as? Int, window > 0 {
            contextTokens = tokens
            contextWindow = window
        }
        historyID = ev["history_id"] as? String ?? ""
        busy = ev["busy"] as? Bool ?? false
        waiting = ev["waiting"] as? Bool ?? false
        partial = ev["partial"] as? String ?? ""
        elapsedMs = ev["elapsed_ms"] as? Int ?? 0
        userText = ev["user_text"] as? String ?? ""
        queued = ev["queued"] as? [String] ?? []
        suggestion = ev["suggestion"] as? String ?? ""
        shellRunning = ev["shell_running"] as? Bool ?? false
        project = ev["project"] as? String ?? ""
        pinned = ev["pinned"] as? Bool ?? false
        created = ev["created"] as? Double ?? 0
        updated = ev["updated"] as? Double ?? 0
    }
}

/// A project on the machine: a goal, a folder and instructions around one chat (vision/projects.py).
/// Its chat opens from the drawer's Projects page, not the list of chats.
/// A chat where every word searched for turned up in one message (GET /search): that message, cut around
/// the first hit. `role` is "title" when only the chat's title matched.
struct SearchHit: Decodable, Identifiable, Equatable {
    let chat: String
    let title: String
    let project: String
    let role: String
    let snippet: String
    var id: String { chat }
}

struct ProjectInfo: Identifiable, Equatable {
    let id: String
    var title: String
    var goal: String
    var instructions: String
    var folder: String         // the first folder, home written as ~ ("" = the machine's default)
    var chat: String           // its chat's id; "" until it is first opened (and stale once that chat is closed)
    var status: String         // "active" or "paused"; archived ones aren't sent
    var updated: Double
    /// How many of its threads are in each state ("working": 2, "waiting_on_you": 1…); nil from a Vision
    /// older than threads, and it may be empty.
    var threads: [String: Int]?
    /// Output tokens its threads have written, all told.
    var tokensOut: Int
    var settings: ProjectSettings
    /// Every project is a scout: its name, colour and look for the buddy, and the mood the machine
    /// works out from its threads. Empty from a Vision older than scouts.
    var scout: ScoutLook
    var mood: String
    /// on_call | checkins | autonomous (its autonomy, from an older Vision).
    var leash: String
    var rules: [ScoutRule]
    /// What it asked leave to run, still waiting on you.
    var approvals: [ScoutApproval]
    var checkin: ScoutCheckins?

    var isPaused: Bool { status == "paused" }
    /// The scout's name, or the project's title before scouts.
    var name: String { scout.name.isEmpty ? (title.isEmpty ? "Untitled scout" : title) : scout.name }

    init?(_ ev: [String: Any]) {
        guard let id = ev["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        title = ev["title"] as? String ?? ""
        goal = ev["goal"] as? String ?? ""
        instructions = ev["instructions"] as? String ?? ""
        folder = ev["folder"] as? String ?? ""
        chat = ev["chat"] as? String ?? ""
        status = ev["status"] as? String ?? "active"
        updated = ev["updated"] as? Double ?? 0
        threads = ev["threads"] as? [String: Int]
        tokensOut = ev["tokens_out"] as? Int ?? 0
        settings = ProjectSettings(ev["settings"] as? [String: Any] ?? [:])
        scout = ScoutLook(ev["scout"] as? [String: Any] ?? [:])
        mood = ev["mood"] as? String ?? "asleep"
        leash = ev["leash"] as? String ?? settings.leash
        rules = (ev["rules"] as? [[String: Any]] ?? []).compactMap(ScoutRule.init)
        approvals = (ev["approvals"] as? [[String: Any]] ?? []).compactMap(ScoutApproval.init)
        checkin = (ev["checkin"] as? [String: Any]).map(ScoutCheckins.init)
    }
}

/// A scout's buddy: its name, colour (a hue in degrees) and trait (antenna, sprout, ears, curl).
struct ScoutLook: Equatable {
    var name: String
    var hue: Double
    var trait: String

    init(_ d: [String: Any]) {
        name = d["name"] as? String ?? ""
        hue = (d["hue"] as? NSNumber)?.doubleValue ?? 205
        trait = d["trait"] as? String ?? "antenna"
    }

    init(name: String, hue: Double, trait: String) {
        (self.name, self.hue, self.trait) = (name, hue, trait)
    }

    var json: [String: Any] { ["name": name, "hue": Int(hue), "trait": trait] }

    static let hues: [Double] = [205, 32, 268, 140, 340, 175, 52, 300]
    static let traits = ["antenna", "sprout", "ears", "curl"]
}

/// One action a scout may take, and whether it does it, asks first or never does (projects.py ACTIONS).
struct ScoutRule: Equatable, Identifiable {
    let action: String
    var label: String
    /// does | asks | never
    var rule: String
    /// Set by the user, not the leash's default.
    var set: Bool
    var id: String { action }

    init?(_ d: [String: Any]) {
        guard let action = d["action"] as? String else { return nil }
        self.action = action
        label = d["label"] as? String ?? action
        rule = d["rule"] as? String ?? "asks"
        set = d["set"] as? Bool ?? false
    }
}

/// A scout asking leave to run something a rule says asks first (projects.py approvals).
struct ScoutApproval: Equatable, Identifiable {
    let id: String
    /// The thread that asked, or "coordinator" (the scout itself).
    var target: String
    /// Who asks: the thread's title, or the scout's name.
    var title: String
    var tool: String
    var command: String
    var action: String
    var created: Double

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, (d["state"] as? String ?? "pending") == "pending" else { return nil }
        self.id = id
        target = d["target"] as? String ?? ""
        title = d["title"] as? String ?? ""
        tool = d["tool"] as? String ?? ""
        command = d["command"] as? String ?? ""
        action = d["action"] as? String ?? ""
        created = d["created"] as? Double ?? 0
    }
}

/// When a scout checks in: its schedule (nil: none), the last check-in, and when the running one started.
struct ScoutCheckins: Equatable {
    struct Schedule: Equatable {
        /// daily | weekdays | weekly | every
        var repeatKind: String
        var time: String
        var everyMinutes: Int
        var enabled: Bool
        var nextRun: Double?
        var weekday: Int = 0   // 0 = Monday, weekly only
    }

    var schedule: Schedule?
    var lastStarted: Double?
    var lastNeeds: Int
    var running: Double?

    init(_ d: [String: Any]) {
        if let s = d["schedule"] as? [String: Any] {
            schedule = Schedule(repeatKind: s["repeat"] as? String ?? "daily", time: s["time"] as? String ?? "08:00",
                                everyMinutes: s["every_minutes"] as? Int ?? 120, enabled: s["enabled"] as? Bool ?? true,
                                nextRun: s["next_run"] as? Double, weekday: s["weekday"] as? Int ?? 0)
        }
        let last = d["last"] as? [String: Any]
        lastStarted = last?["started"] as? Double
        lastNeeds = last?["needs_you"] as? Int ?? 0
        running = d["running"] as? Double
    }

    /// "every morning at 8:00", "weekdays at 9:00", "every 2 hours", "off".
    var words: String {
        guard let s = schedule, s.enabled else { return "off" }
        let at = s.time.hasPrefix("0") ? String(s.time.dropFirst()) : s.time
        switch s.repeatKind {
        case "every":
            let m = s.everyMinutes
            return m % 60 != 0 ? "every \(m) minutes" : m == 60 ? "every hour" : "every \(m / 60) hours"
        case "weekdays": return "weekdays at \(at)"
        case "weekly": return "weekly at \(at)"
        default:
            let h = Int(s.time.prefix(2)) ?? 8
            return "every \(h < 12 ? "morning" : h < 17 ? "afternoon" : "evening") at \(at)"
        }
    }
}

/// How a project runs (projects.py SETTINGS): who coordinates and who does the threads, how many at once,
/// how freely the coordinator starts them, how much it reports, and what reaches the phone.
struct ProjectSettings: Equatable {
    var coordinatorModel = ""   // "" = the chat's own
    var coordinatorEffort = ""
    var threadModel = ""
    var threadEffort = ""
    var maxThreads = 3
    var isolation = "worktree"  // worktree | shared
    var autonomy = "balanced"   // ask | balanced | free (the leash's older name, kept in step with it)
    var leash = "checkins"      // on_call | checkins | autonomous
    var updates = "milestones"  // all | milestones | needs_you
    var notify = "needs_you"    // all | needs_you | none
    var length = "normal"       // short | normal | long: how long one go of a thread may run

    init(_ d: [String: Any] = [:]) {
        length = d["length"] as? String ?? "normal"
        coordinatorModel = d["coordinator_model"] as? String ?? ""
        coordinatorEffort = d["coordinator_effort"] as? String ?? ""
        threadModel = d["thread_model"] as? String ?? ""
        threadEffort = d["thread_effort"] as? String ?? ""
        maxThreads = d["max_threads"] as? Int ?? 3
        isolation = d["isolation"] as? String ?? "worktree"
        autonomy = d["autonomy"] as? String ?? "balanced"
        leash = d["leash"] as? String ?? ["ask": "on_call", "free": "autonomous"][autonomy] ?? "checkins"
        updates = d["updates"] as? String ?? "milestones"
        notify = d["notify"] as? String ?? "needs_you"
    }

    /// As the machine takes them.
    var json: [String: Any] {
        ["coordinator_model": coordinatorModel, "coordinator_effort": coordinatorEffort, "thread_model": threadModel,
         "thread_effort": threadEffort, "max_threads": maxThreads, "isolation": isolation,
         "autonomy": ["on_call": "ask", "autonomous": "free"][leash] ?? "balanced", "leash": leash,
         "updates": updates, "notify": notify, "length": length]
    }
}

/// One message of a thread's conversation, in plain words: "user" is what Vision gave it, "assistant" what it returned.
struct ThreadMessage: Decodable, Hashable {
    let role: String
    let text: String
}

/// A project thread: a background worker that belongs to a project, runs outside any chat turn and reports
/// back (vision/projects.py). The `thread` frame leaves out `runs`; the GET routes carry them.
struct ProjectThread: Codable, Identifiable, Hashable {
    let id: String
    var project: String
    var title: String
    var objective: String
    /// "working", "waiting_on_you", "ready_for_review", "idle" or "resolved".
    var state: String
    /// Working but waiting for a free slot on the machine.
    var queued: Bool
    /// False for a read-only thread.
    var edits: Bool
    var runID: String?
    var provider: String?
    var model: String?
    var effort: String?
    var sessionID: String?
    var workdir: String?
    var folder: String?        // the workdir, home written as ~
    var branch: String?
    var worktree: String?
    var created: Double
    var updated: Double
    /// What it is doing, in plain words ("running tests"); empty when it isn't.
    var activity: String
    var tools: Int
    var lastResult: ThreadResult?
    var question: String?
    var error: String?
    var note: String?
    var tokensOut: Int
    /// Working but held: its project is paused.
    var paused: Bool
    /// What the user last told it directly (the phone's field, a terminal), until it next stops.
    var told: String?
    /// Every run so far, oldest first: only in the full views (GET), never in a frame.
    var runs: [ThreadRun]?

    enum CodingKeys: String, CodingKey {
        case id, project, title, objective, state, queued, edits, provider, model, effort, workdir, folder, branch, worktree,
             created, updated, activity, tools, question, error, note, runs, paused, told
        case runID = "run_id", sessionID = "session_id", lastResult = "last_result", tokensOut = "tokens_out"
    }

    var isOpen: Bool { state != "resolved" }
    var isWorking: Bool { state == "working" }
    var isWaiting: Bool { state == "waiting_on_you" }
    /// Finished work on a branch of its own that can be merged into the project's folder.
    var canLand: Bool { (state == "ready_for_review" || state == "idle") && !(branch ?? "").isEmpty }

    /// Lenient: a field the server leaves out or sends as null takes its empty value, so one odd key never
    /// drops the whole thread.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        project = c.lenient(.project, "")
        title = c.lenient(.title, "")
        objective = c.lenient(.objective, "")
        state = c.lenient(.state, "idle")
        queued = c.lenient(.queued, false)
        edits = c.lenient(.edits, true)
        runID = c.lenient(.runID)
        provider = c.lenient(.provider)
        model = c.lenient(.model)
        effort = c.lenient(.effort)
        sessionID = c.lenient(.sessionID)
        workdir = c.lenient(.workdir)
        folder = c.lenient(.folder)
        branch = c.lenient(.branch)
        worktree = c.lenient(.worktree)
        created = c.lenient(.created, 0)
        updated = c.lenient(.updated, 0)
        activity = c.lenient(.activity, "")
        tools = c.lenient(.tools, 0)
        lastResult = c.lenient(.lastResult)
        question = c.lenient(.question)
        error = c.lenient(.error)
        note = c.lenient(.note)
        tokensOut = c.lenient(.tokensOut, 0)
        paused = c.lenient(.paused, false)
        told = c.lenient(.told)
        runs = c.lenient(.runs)
    }

    /// From a frame's dictionary.
    init?(_ json: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: json),
              let thread = try? JSONDecoder().decode(ProjectThread.self, from: data) else { return nil }
        self = thread
    }
}

/// What a thread's last run came back with.
struct ThreadResult: Codable, Hashable {
    var status: String
    var summary: String
    var changes: [String]
    var checks: [String]
    var findings: [String]
    var question: String?
    /// What later work in the project must know (they go into the project memory).
    var decisions: [String]
    /// Files it made for the user to look at (they go into the project's Library).
    var artifacts: [String]

    enum CodingKeys: String, CodingKey { case status, summary, changes, checks, findings, question, decisions, artifacts }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = c.lenient(.status, "")
        summary = c.lenient(.summary, "")
        changes = c.lenient(.changes, [])
        checks = c.lenient(.checks, [])
        findings = c.lenient(.findings, [])
        question = c.lenient(.question)
        decisions = c.lenient(.decisions, [])
        artifacts = c.lenient(.artifacts, [])
    }
}

/// One run of a thread: its start, a message that continued it, an answer, or a resume after a restart.
struct ThreadRun: Codable, Hashable {
    var at: Double
    /// "start", "continue", "answer" or "resume".
    var kind: String
    var status: String
    var summary: String
    var findings: [String]
    var changes: [String]
    var durationS: Double?
    var tokensOut: Int?

    enum CodingKeys: String, CodingKey {
        case at, kind, status, summary, findings, changes
        case durationS = "duration_s", tokensOut = "tokens_out"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        at = c.lenient(.at, 0)
        kind = c.lenient(.kind, "")
        status = c.lenient(.status, "")
        summary = c.lenient(.summary, "")
        findings = c.lenient(.findings, [])
        changes = c.lenient(.changes, [])
        durationS = c.lenient(.durationS)
        tokensOut = c.lenient(.tokensOut)
    }
}

/// What landing a thread would bring into the project's folder (GET …/threads/{id}/diff): its commits and
/// the files they change, biggest first; with a path asked for, that file's patch too.
struct ThreadDiff: Decodable, Hashable {
    struct File: Decodable, Hashable, Identifiable {
        var path: String
        /// Lines added and removed; -1 for a binary file.
        var added: Int
        var removed: Int
        var id: String { path }
    }

    var base: String
    var commits: Int
    var files: [File]
    /// Files left out past the machine's cap.
    var more: Int
    var patch: String?
    /// The patch was cut short.
    var cut: Bool?
}

private extension KeyedDecodingContainer {
    /// The value under `key`, or `fallback` when it is missing, null or of another type.
    func lenient<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        (try? decodeIfPresent(T.self, forKey: key)) ?? fallback
    }

    func lenient<T: Decodable>(_ key: Key) -> T? {
        try? decodeIfPresent(T.self, forKey: key)
    }
}

/// "84k", "1.2M": token counts the way the terminal prints them.
func tokenText(_ n: Int) -> String {
    switch n {
    case ..<1000: return "\(n)"
    case ..<10_000: return String(format: "%.1fk", Double(n) / 1000)
    case ..<1_000_000: return "\(n / 1000)k"
    default: return String(format: "%.1fM", Double(n) / 1_000_000)
    }
}

/// How the app and the Vision it talks to have drifted apart, from hello's "protocol" and "update"
/// (what to run on the computer to update it, when a newer Vision is out).
/// `server` is the one that breaks things: this app asks for what that Vision does not have yet.
enum Staleness: Equatable {
    case server(how: String?)   // the computer's Vision is older than this app needs
    case app                    // this app is older than the computer's Vision
    case update(how: String)    // they fit, and a newer Vision is out for the computer

    /// A Vision from before the number says nothing: it is protocol 1, everything up to the number.
    init?(protocol theirs: Int?, update how: String?, appProtocol: Int) {
        let theirs = theirs ?? 1
        if theirs < appProtocol {
            self = .server(how: how)
        } else if theirs > appProtocol {
            self = .app
        } else if let how {
            self = .update(how: how)
        } else {
            return nil
        }
    }

    /// Whether something in the app won't work until one side is updated (an update merely out won't).
    var breaks: Bool {
        if case .update = self { return false }
        return true
    }

    var title: String {
        switch self {
        case .server: "Vision on your computer is out of date"
        case .app: "This app is out of date"
        case .update: "An update is ready for your computer"
        }
    }

    var detail: String {
        switch self {
        case .server(let how): "This app is newer than it, so some things here won't work. \(Self.steps(how))"
        case .app: "The Vision on your computer is newer than this app. Update the app."
        case .update(let how): Self.steps(how)
        }
    }

    /// As markdown, so each command reads as one. `how` is the whole command, restart included; a
    /// Vision too old to say how gets both ways.
    private static func steps(_ how: String?) -> String {
        guard let how else {
            return "On the computer, run Vision's installer again (or `git pull` where it lives), then `vision restart`."
        }
        return "On the computer, run `\(how)`."
    }

    /// Said over the chat on connecting: once when something won't work, once a day for an update that
    /// is merely out. Settings keeps saying it either way.
    var toast: String {
        switch self {
        case .server: "Vision on your computer is out of date. Settings says how to update it."
        case .app: "This app is out of date. Update it to keep everything working."
        case .update: "An update is ready for your computer. Settings says how to get it."
        }
    }
}

struct Hello: Decodable {
    struct ChatSummary: Decodable {
        let chat: String
        let model: String
        let effort: String
        let provider: String
    }
    let version: String
    let workdir: String
    let chats: [ChatSummary]
    /// What it runs for speech; nil from a Vision older than the status report.
    let status: ServerStatus?
}

/// What a Vision runs for speech (/health "status"): rows and problems in the server's own words,
/// shown as they are, so a new check on the server needs no app update.
struct ServerStatus: Decodable, Equatable {
    struct Row: Decodable, Equatable, Identifiable {
        let label: String
        let value: String
        /// "ready", "loading", "failed" or "off".
        let state: String
        var id: String { label }
    }
    let rows: [Row]
    let problems: [String]

    var isLoading: Bool { rows.contains { $0.state == "loading" } }
}

extension ChatInfo {
    /// When the running reply began, by this phone's clock (the machine says how long it has been going).
    var replyBegan: Date { Date().addingTimeInterval(-Double(elapsedMs) / 1000) }
}

/// One folder on the machine, as the new-chat picker walks them (GET /folders).
struct FolderListing: Decodable {
    struct Place: Decodable, Identifiable {
        let path: String
        let label: String       // the path with home written as ~
        var id: String { path }
    }
    struct Child: Decodable, Identifiable {
        let name: String
        let path: String
        var id: String { path }
    }
    let path: String
    let label: String
    let parent: String          // "" at the top
    let folders: [Child]
    let recent: [Place]?        // only where the picker starts: the default folder and where work has been
    let more: Int?              // subfolders left out of a very large folder
}

struct HistoryItem: Decodable {
    let role: String
    let text: String
    let error: String?  // a failed turn (usage limit, crash): shown in red like the live `done` event
    let agents: [AgentFrame]?  // the subagents a reply ran, kept with the conversation on the machine
    let tools: [ToolFrame]?    // …and its tool calls (while the machine's server is up)
    let duration_ms: Int?      // how long the turn took (replies finished while the machine's server has been up)
    let output: String?        // a shell row: what the command printed
    let code: Int?             // …and its exit code (null: stopped)
    let running: Bool?
    let label: String?         // a check-in row: "Morning check-in"…
    let started: Double?
    let ended: Double?
    let needs_you: Int?
}

/// A scout's check-in as the chat shows it.
struct CheckinCard: Equatable {
    var label: String
    var started: Double?
    var ended: Double?
    var needsYou: Int
}

/// A tool call as the machine keeps it with a past reply (the same fields as a live `tool` frame).
struct ToolFrame: Decodable {
    let id: String
    let name: String?
    let detail: String?
    let done: Bool?
    let is_error: Bool?
    let output: String?
    let at: Int?
    let diff: [DiffHunk]?
    let input: [[String]]?
}

extension ToolRow {
    init(_ f: ToolFrame) {
        self.init(id: f.id, at: f.at ?? 0)
        name = f.name ?? ""
        detail = f.detail ?? ""
        done = true   // a kept call is over, including one cut off by a crash
        isError = (f.is_error ?? false) || !(f.done ?? true)
        output = f.output ?? ""
        diff = f.diff ?? []
        input = (f.input ?? []).compactMap(ToolInput.init)
        if f.done == false && output.isEmpty { output = "Interrupted while this tool was running" }
    }
}

/// A subagent row as the machine keeps it with a past reply (the same fields as a live `agent` frame).
struct AgentFrame: Decodable {
    let id: String
    let kind: String?
    let label: String?
    let model: String?
    let effort: String?
    let tools: Int?
    let tokens: Int?
    let started: Double?
    let done: Bool?
    let failed: Bool?
    let cut_off: Bool?
    let status: String?
    let at: Int?
    let steps: [AgentStep]?
    let step: AgentStep?
    let prompt: String?
    let summary: String?
    let details: [String]?
}

extension AgentRow {
    init(_ f: AgentFrame) {
        self.init(id: f.id, at: f.at ?? 0, started: (f.started ?? 0) > 0 ? Date(timeIntervalSince1970: f.started!) : .now)
        kind = f.kind ?? kind
        label = f.label ?? ""
        model = f.model ?? ""
        effort = f.effort ?? ""
        tools = f.tools ?? 0
        tokens = f.tokens ?? 0
        steps = f.steps ?? f.step.map { [$0] } ?? []
        prompt = f.prompt ?? ""
        summary = f.summary ?? ""
        details = f.details ?? []
        // A kept row that never finished was cut off: nothing will finish it now.
        done = true
        failed = f.failed ?? !(f.done ?? false)
        cutOff = f.cut_off ?? !(f.done ?? false)
        status = f.status ?? ""
    }

    /// An agent's running time, `12s`, `6m 22s`, `1h 04m`: the terminal's clock.
    static func clock(_ seconds: Int) -> String {
        let s = max(0, seconds)
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m \(String(format: "%02d", s % 60))s" }
        return "\(s / 3600)h \(String(format: "%02d", s / 60 % 60))m"
    }
}

struct ModelCatalogue: Decodable {
    struct Entry: Decodable, Identifiable {
        let id: String
        let label: String
        let description: String
    }

    struct Tab: Decodable, Identifiable {
        var id: String { tab }
        let tab: String
        let models: [Entry]
    }

    let tabs: [Tab]
    let efforts: [Entry]
    let current: String
    let effort: String
}

/// The voices the machine can speak in now ("designed", "cloned" or nothing as the note) and the one in use.
struct VoiceCatalogue: Decodable {
    struct Entry: Decodable, Identifiable {
        let id: String
        let note: String
        /// There is a clip of it to hear before choosing it; nil from a Vision before samples.
        let sample: Bool?
    }

    let voice: String
    let voices: [Entry]
}

struct SessionRow: Decodable, Identifiable {
    let id: String
    let title: String
    let age: String
    let current: Bool          // already open as a chat
    let chat: String?
    let provider: String
    let updated: Double?       // epoch seconds; absent from older machines
    var model: String? = nil   // "Opus 5.5", when the machine knows what it last ran on
    var effort: String? = nil
    var stableID: String { "\(provider):\(id)" }

    /// "Opus 5.5 medium", else the provider's name.
    var brainLabel: String {
        guard let model, !model.isEmpty else { return ProviderStyle.title(provider) }
        return [model, effort].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
    }

    /// A photo sent as the first message titles the chat "[Attached image: /long/path…]"; show
    /// "Photo" or "File" (plus whatever was typed with it) instead of the machine's file path.
    var displayTitle: String {
        guard title.hasPrefix("[Attached") else { return title.isEmpty ? "Untitled" : title }
        let kind = title.hasPrefix("[Attached file") ? "File" : "Photo"
        guard let end = title.firstIndex(of: "]") else { return kind }
        let rest = title[title.index(after: end)...].trimmingCharacters(in: .whitespaces)
        return rest.isEmpty ? kind : rest
    }

    var hasAttachment: Bool { title.hasPrefix("[Attached") }

    /// Epoch seconds for ordering: the timestamp, else estimated from the age text an older
    /// machine sends ("just now", "12 min ago", "3 h ago", "yesterday", "4 days ago", "Sep 12").
    var sortKey: Double {
        if let updated { return updated }
        let now = Date().timeIntervalSince1970
        let words = age.split(separator: " ")
        let n = Double(words.first ?? "") ?? 0
        if age == "just now" { return now }
        if age == "yesterday" { return now - 86400 }
        if age.hasSuffix("min ago") { return now - n * 60 }
        if age.hasSuffix("h ago") { return now - n * 3600 }
        if age.hasSuffix("days ago") { return now - n * 86400 }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "MMM dd yyyy"
        let year = Calendar.current.component(.year, from: .now)
        guard var date = f.date(from: "\(age) \(year)") else { return 0 }
        if date > .now, let last = Calendar.current.date(byAdding: .year, value: -1, to: date) { date = last }
        return date.timeIntervalSince1970
    }
}

/// `/usage` on the machine: one card per subscription provider, each with its rate-limit windows.
struct UsageReport: Decodable {
    struct Window: Decodable, Identifiable {
        var id: String { label }
        let label: String
        let usedPercent: Double?
        let resetsAt: Double?
        let resets: String?

        enum CodingKeys: String, CodingKey {
            case label, resets
            case usedPercent = "used_percent"
            case resetsAt = "resets_at"
        }
    }

    /// Saved usage-limit resets (Claude's `/limit-reset` grants, Codex's banked resets).
    struct Banked: Decodable {
        let count: Int
        let expiresAt: Double?
        let label: String?
        var resets: [Group]? = nil  // one per expiry date, soonest first; absent from older machines

        /// Resets of one kind sharing one expiry.
        struct Group: Decodable {
            let count: Int
            let expiresAt: Double?
            var kind: String? = nil  // "Full Reset", "Session Reset", "Fable Reset"…; nil when unknown

            enum CodingKeys: String, CodingKey {
                case count, kind
                case expiresAt = "expires_at"
            }
        }

        /// Always at least one group: older machines only sent the soonest expiry.
        var groups: [Group] {
            if let resets, !resets.isEmpty { return resets }
            return [Group(count: count, expiresAt: expiresAt)]
        }

        /// One saved reset, with the id that spends exactly it (nil: the provider picks).
        struct Item: Decodable {
            var id: String? = nil
            var kind: String? = nil
            let expiresAt: Double?

            enum CodingKeys: String, CodingKey {
                case id, kind
                case expiresAt = "expires_at"
            }
        }

        var items: [Item]? = nil  // soonest expiry first; absent from older machines

        /// One row per reset: the machine's list, or the groups spelt out one by one.
        var each: [Item] {
            if let items, !items.isEmpty { return items }
            return groups.flatMap { g in Array(repeating: Item(kind: g.kind, expiresAt: g.expiresAt), count: g.count) }
        }

        enum CodingKeys: String, CodingKey {
            case count, label, resets, items
            case expiresAt = "expires_at"
        }
    }

    struct Provider: Decodable, Identifiable {
        var id: String { provider }
        let provider: String
        let label: String
        let plan: String?
        let windows: [Window]
        var banked: Banked? = nil  // absent from older machines
        let notes: [String]
        let source: String?
        let at: Double?
        let error: String?
    }

    var providers: [Provider]
    let at: Double

    /// The machine's answer to spending a banked reset, with that provider's usage read again after.
    struct ResetResult: Decodable {
        let ok: Bool
        let outcome: String
        let message: String
        let provider: Provider
    }
}

/// One thing in the Library: a picture a reply showed (`source` "vision") or something sent from the phone ("you").
struct LibraryItem: Decodable, Identifiable, Hashable {
    let kind: String        // image | video | file
    let source: String      // vision | you | thread (a project thread made it)
    let path: String
    let name: String
    let caption: String
    let time: Double
    let size: Int
    var thread: String? = nil   // which project thread made it
    var id: String { path }
    var date: Date { Date(timeIntervalSince1970: time) }
}

/// A prompt the machine runs on its own at a set time, each run in a new chat.
struct ScheduledTask: Codable, Identifiable, Hashable {
    var id: String = ""
    var title: String = ""
    var prompt: String = ""
    var repeatRule: String = "daily"   // once | daily | weekdays | weekly
    var time: String = "09:00"         // HH:MM, the machine's local time
    var date: String? = nil            // YYYY-MM-DD, once only
    var weekday: Int? = nil            // 0 = Monday, weekly only
    var enabled: Bool = true
    var lastRun: Double? = nil
    var nextRun: Double? = nil
    var lastChat: String? = nil
    var workdir: String? = nil         // the folder a run works in; nil or "" is the machine's default
    var kind: String? = nil            // "reminder": the words come back as they are, no model runs (set from a chat);
                                       // "checkin": a scout's status report, kept and edited on the scout, not here

    var isReminder: Bool { kind == "reminder" }
    var isScoutReport: Bool { kind == "checkin" }

    enum CodingKeys: String, CodingKey {
        case id, title, prompt, time, date, weekday, enabled, kind, workdir
        case repeatRule = "repeat", lastRun = "last_run", nextRun = "next_run", lastChat = "last_chat"
    }

    var body: [String: Any] {
        var b: [String: Any] = ["title": title, "prompt": prompt, "repeat": repeatRule, "time": time, "enabled": enabled,
                                "workdir": workdir ?? ""]
        if !id.isEmpty { b["id"] = id }
        if let date { b["date"] = date }
        if let weekday { b["weekday"] = weekday }
        return b
    }

    /// "Every day at 8:30", "Weekdays at 7:00", "Sundays at 18:00", "Once, 26 Sep at 9:00".
    var summary: String {
        let clock = Self.clockText(time)
        switch repeatRule {
        case "daily": return "Every day at \(clock)"
        case "weekdays": return "Weekdays at \(clock)"
        case "weekly":
            let names = Calendar.current.standaloneWeekdaySymbols   // Sunday first
            let name = names[((weekday ?? 0) + 1) % 7]
            return "\(name)s at \(clock)"
        default:
            guard let date, let d = Self.dayFormat.date(from: date) else { return "Once at \(clock)" }
            return "Once, \(d.formatted(.dateTime.day().month(.abbreviated))) at \(clock)"
        }
    }

    static let dayFormat: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    static func clockText(_ hhmm: String) -> String {
        let parts = hhmm.split(separator: ":").compactMap { Int($0) }
        guard parts.count >= 2, let d = Calendar.current.date(bySettingHour: parts[0], minute: parts[1], second: 0, of: Date()) else { return hhmm }
        return d.formatted(date: .omitted, time: .shortened)
    }
}

enum ClientError: LocalizedError {
    case notConfigured
    case http(Int, String)
    case badResponse

    var errorDescription: String? {
        switch self {
        case .notConfigured: return "Pair with Vision in Settings first."
        case .http(let code, let detail): return code == 401 ? "The access key wasn't accepted." : "Vision replied \(code): \(detail)"
        case .badResponse: return "Unexpected reply from Vision."
        }
    }
}

/// A queued message taken back to edit: its text and attachments, for the composer.
struct EditDraft: Equatable {
    let text: String
    let attachments: [Attachment]
}

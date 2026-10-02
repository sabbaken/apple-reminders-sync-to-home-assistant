// EventKit reads every provider already connected to Calendar.app.
import Foundation
import EventKit
import IOKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}
let arguments = CommandLine.arguments
if arguments.count != 3 { fail("usage: CalendarExport PAST_DAYS FUTURE_DAYS") }
guard let past = Int(arguments[1]), let future = Int(arguments[2]),
      past >= 0, future > 0, past + future <= 1460 else {
    fail("Calendar range must be positive and no longer than four years")
}
let store = EKEventStore()
var finished = false
var allowed = false
var accessError: Error?
let completion: (Bool, Error?) -> Void = { granted, error in
    DispatchQueue.main.async {
        allowed = granted
        accessError = error
        finished = true
    }
}
if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents(completion: completion)
} else {
    store.requestAccess(to: .event, completion: completion)
}
let deadline = Date().addingTimeInterval(90)
while !finished && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}
guard finished && allowed else {
    fail("Calendar access denied or timed out. Allow Apple Calendar Sync in System Settings > Privacy & Security > Calendars. " + (accessError?.localizedDescription ?? ""))
}
store.reset()
let local = Calendar.current
let today = local.startOfDay(for: Date())
let start = local.date(byAdding: .day, value: -past, to: today)!
let end = local.date(byAdding: .day, value: future, to: today)!
let iso = ISO8601DateFormatter()
let day = DateFormatter()
day.locale = Locale(identifier: "en_US_POSIX")
day.calendar = Calendar(identifier: .gregorian)
day.timeZone = TimeZone.current
day.dateFormat = "yyyy-MM-dd"
let calendars = store.calendars(for: .event)
// A refused read must never replace a valid snapshot with an empty one.
guard !calendars.isEmpty else { fail("EventKit returned no calendars; refusing to publish an empty snapshot") }
var exported: [[String: Any]] = []
for calendar in calendars {
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: [calendar])
    let events = store.events(matching: predicate).filter { $0.status != .canceled }
        .sorted { $0.startDate < $1.startDate }
    var rows: [[String: Any]] = []
    for event in events {
        // All-day dates are floating dates, with an exclusive end, not UTC midnights.
        let format: (Date) -> String = event.isAllDay ? { day.string(from: $0) } : { iso.string(from: $0) }
        var row: [String: Any] = [
            "uid": (event.calendarItemIdentifier) + "@" + iso.string(from: event.startDate),
            "summary": event.title ?? "",
            "start": format(event.startDate), "end": format(event.endDate)
        ]
        if let notes = event.notes { row["description"] = notes }
        if let location = event.location { row["location"] = location }
        rows.append(row)
    }
    exported.append(["id": calendar.calendarIdentifier, "name": calendar.title,
                     "source": calendar.source.title, "events": rows])
}
let platform = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
let machineID = platform == 0 ? nil : IORegistryEntryCreateCFProperty(platform, "IOPlatformUUID" as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() as? String
if platform != 0 { IOObjectRelease(platform) }
guard let machineID = machineID else { fail("Cannot determine a stable Mac identifier") }
let data: [String: Any] = ["version": 1, "source_id": machineID,
                         "range_start": iso.string(from: start), "range_end": iso.string(from: end),
                         "calendars": exported]
do {
    let json = try JSONSerialization.data(withJSONObject: data, options: [.sortedKeys])
    FileHandle.standardOutput.write(json)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch { fail(error.localizedDescription) }

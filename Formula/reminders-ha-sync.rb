# Homebrew formula for the Reminders <-> Home Assistant sync.
#
# This is the canonical copy. To publish it, either:
#
#   a) copy it into a tap repo -- github.com/<you>/homebrew-tap, in Formula/ --
#      which gives users `brew install <you>/tap/reminders-ha-sync`, or
#   b) tap this repo directly, no second repo needed:
#        brew tap sabbaken/tap https://github.com/sabbaken/apple-reminders-sync-to-home-assistant
#        brew install sabbaken/tap/reminders-ha-sync
#
# `make formula TAG=v0.1.0` prints this file with the release's real sha256
# filled in.
class RemindersHaSync < Formula
  desc "Two-way sync between macOS Reminders and Home Assistant to-do lists"
  homepage "https://github.com/sabbaken/apple-reminders-sync-to-home-assistant"
  url "https://github.com/sabbaken/apple-reminders-sync-to-home-assistant/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "AGPL-3.0-only"
  head "https://github.com/sabbaken/apple-reminders-sync-to-home-assistant.git", branch: "master"

  # Reminders is a macOS app; there is nothing to sync anywhere else.
  depends_on :macos

  # The thing that actually talks to the Reminders app. Cross-tap dependencies
  # are fine -- Homebrew taps keith/formulae on demand.
  depends_on "keith/formulae/reminders-cli"

  # No Python dependency on purpose: the script is standard-library only and
  # runs under the /usr/bin/python3 macOS ships, which is also the interpreter
  # the LaunchAgent names. Depending on a Homebrew Python would put a path in
  # that plist that a later `brew uninstall` could take away.

  def install
    bin.install "reminders_ha_sync.py" => "reminders-ha-sync"
    # The script is installed on its own, so keep the licence next to it.
    prefix.install "LICENSE"
  end

  def caveats
    <<~EOS
      One more step -- this asks for your Home Assistant address and token,
      shows you which lists it will sync, and starts syncing in the background:

        reminders-ha-sync setup

      You will need a long-lived access token from an administrator account:
      Home Assistant -> your profile -> Security -> Long-lived access tokens.

      macOS will ask to let reminders-cli read your Reminders. Say yes; the
      dialog only appears for a command you run yourself, so answer it during
      setup rather than waiting for a background sync.

      Check on it later with:  reminders-ha-sync doctor
      Stop it with:            reminders-ha-sync uninstall
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/reminders-ha-sync --version")
    assert_match "Two-way sync", shell_output("#{bin}/reminders-ha-sync --help")
    # `setup` has to work before any config exists -- that is its whole job.
    assert_match "guided", shell_output("#{bin}/reminders-ha-sync --help")
  end
end

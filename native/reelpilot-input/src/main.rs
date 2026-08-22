#![cfg_attr(not(target_os = "windows"), allow(dead_code))]

#[cfg(target_os = "windows")]
mod windows_helper {
    use std::hint::spin_loop;
    use std::io::{self, Read, Write};
    use std::sync::mpsc::{self, TryRecvError};
    use std::thread;
    use std::time::{Duration, Instant};

    use windows_sys::Win32::Foundation::{HWND, RECT};
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        INPUT, INPUT_0, INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEINPUT,
        SendInput,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetWindowRect, IsWindow, SetCursorPos, SetForegroundWindow,
    };

    const PROTOCOL_VERSION: u8 = 1;
    const OP_IDLE: u8 = 0;
    const OP_DUTY: u8 = 1;
    const OP_PRESS: u8 = 2;
    const OP_RELEASE: u8 = 3;
    const OP_SHUTDOWN: u8 = 4;
    const CONTROL_INTERVAL: Duration = Duration::from_millis(20);

    #[derive(Clone, Copy, Debug, PartialEq)]
    enum Command {
        Idle,
        Duty(f32),
        Press,
        Release,
        Shutdown,
    }

    struct MouseGuard {
        is_down: bool,
    }

    impl MouseGuard {
        fn new() -> Self {
            Self { is_down: false }
        }

        fn set(&mut self, down: bool) -> io::Result<()> {
            if self.is_down == down {
                return Ok(());
            }
            send_mouse(down)?;
            self.is_down = down;
            Ok(())
        }
    }

    impl Drop for MouseGuard {
        fn drop(&mut self) {
            let _ = send_mouse(false);
            self.is_down = false;
        }
    }

    pub fn run() -> i32 {
        let window_handle = match parse_window_handle() {
            Ok(value) => value,
            Err(message) => {
                eprintln!("{message}");
                return 2;
            }
        };
        if unsafe { IsWindow(window_handle) } == 0 {
            eprintln!("invalid Stardew Valley window handle");
            return 2;
        }
        if let Err(error) = prepare_window(window_handle) {
            eprintln!("failed to prepare game window: {error}");
            return 3;
        }

        let (sender, receiver) = mpsc::channel();
        thread::spawn(move || read_commands(sender));
        println!("READY {PROTOCOL_VERSION}");
        let _ = io::stdout().flush();

        let guarded = std::panic::catch_unwind(|| {
            let mut mouse = MouseGuard::new();
            let mut current = Command::Idle;
            loop {
                loop {
                    match receiver.try_recv() {
                        Ok(command) => current = command,
                        Err(TryRecvError::Disconnected) => {
                            current = Command::Shutdown;
                            break;
                        }
                        Err(TryRecvError::Empty) => break,
                    }
                }
                match current {
                    Command::Shutdown => break,
                    Command::Idle | Command::Release => {
                        let _ = mouse.set(false);
                        current = Command::Idle;
                        thread::sleep(Duration::from_millis(2));
                    }
                    Command::Press => {
                        let _ = prepare_window(window_handle);
                        let _ = mouse.set(true);
                        thread::sleep(Duration::from_millis(2));
                    }
                    Command::Duty(duty_ratio) => {
                        let cycle_started = Instant::now();
                        let duty = duty_ratio.clamp(0.0, 1.0);
                        if duty > 0.0 {
                            let _ = prepare_window(window_handle);
                            let _ = mouse.set(true);
                            wait_until(cycle_started + CONTROL_INTERVAL.mul_f32(duty));
                        }
                        let _ = mouse.set(false);
                        wait_until(cycle_started + CONTROL_INTERVAL);
                    }
                }
            }
        });
        let _ = send_mouse(false);
        if guarded.is_err() {
            eprintln!("input helper recovered from an internal panic");
            return 4;
        }
        0
    }

    fn parse_window_handle() -> Result<HWND, String> {
        let value = std::env::args().nth(1).ok_or("missing window handle")?;
        let numeric = value
            .parse::<isize>()
            .map_err(|_| "invalid window handle")?;
        Ok(numeric as HWND)
    }

    fn read_commands(sender: mpsc::Sender<Command>) {
        let mut input = io::stdin().lock();
        read_commands_from(&mut input, sender);
    }

    fn read_commands_from<R: Read>(input: &mut R, sender: mpsc::Sender<Command>) {
        loop {
            let command = match read_command(input) {
                Ok(command) => command,
                Err(_) => Command::Shutdown,
            };
            let shutdown = matches!(command, Command::Shutdown);
            if sender.send(command).is_err() || shutdown {
                return;
            }
        }
    }

    fn read_command<R: Read>(input: &mut R) -> io::Result<Command> {
        let mut opcode = [0_u8; 1];
        input.read_exact(&mut opcode)?;
        match opcode[0] {
            OP_IDLE => Ok(Command::Idle),
            OP_DUTY => {
                let mut bytes = [0_u8; 4];
                input.read_exact(&mut bytes)?;
                let duty = f32::from_le_bytes(bytes);
                if duty.is_finite() {
                    Ok(Command::Duty(duty.clamp(0.0, 1.0)))
                } else {
                    Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "duty ratio must be finite",
                    ))
                }
            }
            OP_PRESS => Ok(Command::Press),
            OP_RELEASE => Ok(Command::Release),
            OP_SHUTDOWN => Ok(Command::Shutdown),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unknown command opcode",
            )),
        }
    }

    fn prepare_window(window_handle: HWND) -> io::Result<()> {
        let mut bounds = RECT::default();
        if unsafe { GetWindowRect(window_handle, &mut bounds) } == 0 {
            return Err(io::Error::last_os_error());
        }
        unsafe {
            SetCursorPos(bounds.right - 30, bounds.bottom - 30);
            SetForegroundWindow(window_handle);
        }
        Ok(())
    }

    fn send_mouse(down: bool) -> io::Result<()> {
        let flags = if down {
            MOUSEEVENTF_LEFTDOWN
        } else {
            MOUSEEVENTF_LEFTUP
        };
        let input = INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT {
                    dx: 0,
                    dy: 0,
                    mouseData: 0,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        };
        let sent = unsafe { SendInput(1, &input, std::mem::size_of::<INPUT>() as i32) };
        if sent == 1 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn wait_until(deadline: Instant) {
        loop {
            let now = Instant::now();
            if now >= deadline {
                return;
            }
            let remaining = deadline - now;
            if remaining > Duration::from_millis(1) {
                thread::sleep(remaining - Duration::from_millis(1));
            } else {
                spin_loop();
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::io::Cursor;

        #[test]
        fn duty_commands_are_clamped() {
            for (input, expected) in [(-0.5_f32, 0.0_f32), (0.4, 0.4), (1.5, 1.0)] {
                let mut bytes = vec![OP_DUTY];
                bytes.extend_from_slice(&input.to_le_bytes());
                let command = read_command(&mut Cursor::new(bytes)).expect("valid duty");
                assert_eq!(command, Command::Duty(expected));
            }
        }

        #[test]
        fn non_finite_duty_and_unknown_opcode_are_rejected() {
            let mut nan = vec![OP_DUTY];
            nan.extend_from_slice(&f32::NAN.to_le_bytes());
            assert_eq!(
                read_command(&mut Cursor::new(nan))
                    .expect_err("NaN must fail")
                    .kind(),
                io::ErrorKind::InvalidData
            );
            assert_eq!(
                read_command(&mut Cursor::new(vec![0xff]))
                    .expect_err("unknown opcode must fail")
                    .kind(),
                io::ErrorKind::InvalidData
            );
        }

        #[test]
        fn eof_and_protocol_failure_become_shutdown() {
            let mut bytes = vec![OP_PRESS, OP_RELEASE, 0xff];
            let (sender, receiver) = mpsc::channel();
            read_commands_from(&mut Cursor::new(&mut bytes), sender);
            assert_eq!(
                receiver.into_iter().collect::<Vec<_>>(),
                vec![Command::Press, Command::Release, Command::Shutdown,]
            );
        }
    }
}

#[cfg(target_os = "windows")]
fn main() {
    std::process::exit(windows_helper::run());
}

#[cfg(not(target_os = "windows"))]
fn main() {
    eprintln!("reelpilot-input supports Windows only");
    std::process::exit(2);
}

//! Options implemented and measured by the MoM runtime.
use std::process::Command;

pub fn apply(command: &mut Command, gpu_vertices: bool) {
    command.env("NWII_WALLTB", "1").env("NWII_SINGLE_VI", "1");
    if gpu_vertices {
        command.env("NWII_GPU_VERTEX", "1").env("NWII_PERSISTENT_UPLOAD", "1");
        for key in ["NWII_NO_GPU_VERTEX", "NWII_NO_GPU_TEXGEN", "NWII_NO_FUSED_BATCH",
                    "NWII_NO_GPU_INPUT_PACKING", "NWII_NO_GPU_UNIFORM_STREAM", "NWII_NO_PERSISTENT_UPLOAD"] {
            command.env_remove(key);
        }
    } else {
        command.env_remove("NWII_GPU_VERTEX").env_remove("NWII_PERSISTENT_UPLOAD");
        command.env("NWII_NO_GPU_VERTEX", "1");
    }
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn gpu_choice_overrides_inherited_rollback_and_can_be_disabled() {
        let mut cmd=Command::new("unused");
        cmd.env("NWII_NO_GPU_VERTEX","1");apply(&mut cmd,true);
        let value=|c:&Command,key:&str| c.get_envs().find(|(k,_)| *k==key).map(|(_,v)|v.map(|s|s.to_string_lossy().into_owned()));
        assert_eq!(value(&cmd,"NWII_GPU_VERTEX"),Some(Some("1".into())));
        assert_eq!(value(&cmd,"NWII_NO_GPU_VERTEX"),Some(None));
        apply(&mut cmd,false);
        assert_eq!(value(&cmd,"NWII_GPU_VERTEX"),Some(None));
        assert_eq!(value(&cmd,"NWII_NO_GPU_VERTEX"),Some(Some("1".into())));
        assert_eq!(value(&cmd,"NWII_SINGLE_VI"),Some(Some("1".into())));
    }
}

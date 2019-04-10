use super::Glean;

pub enum Lifetime {
    /// The metric is reset with each sent ping
    Ping,
    /// The metric is reset on application restart
    Application,
    /// The metric is reset with each user profile
    User
}

impl Default for Lifetime {
    fn default() -> Self {
        Lifetime::Ping
    }
}

#[derive(Default)]
pub struct CommonMetricData {
    pub name: String,
    pub category: String,
    pub send_in_pings: Vec<String>,
    pub lifetime: Lifetime,
    pub disabled: bool,
}

impl CommonMetricData {
    pub fn fullname(&self) -> String {
        format!("{}.{}", self.category, self.name)
    }

    pub fn should_record(&self) -> bool {
        if self.disabled || !Glean::singleton().is_upload_enabled() {
            return false;
        }

        return true;
    }

    pub fn storage_names(&self) -> &[String] {
        &self.send_in_pings
    }
}

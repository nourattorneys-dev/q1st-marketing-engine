<?php
/**
 * Plugin Name: Q1st Connector
 * Description: Lets the Quantum1st marketing engine publish approved articles to this site: signs it in on hosts that drop the login header, writes the SEO title and description, and reports what the site has.
 * Version: 0.1.0
 * Requires at least: 5.6
 * Requires PHP: 7.4
 * Author: Quantum1st
 * License: GPLv2 or later
 */

if (!defined('ABSPATH')) { exit; }

define('Q1ST_CONNECTOR_VERSION', '0.1.0');

/*
 * Sign-in. Many hosts remove the standard Authorization header before WordPress sees it, so the engine also sends the
 * same "Basic user:application-password" value in X-Q1st-Auth. It only fills the fields WordPress already reads; WordPress
 * itself still checks the application password, so a wrong one is refused as usual.
 */
(function () {
    $h = isset($_SERVER['HTTP_X_Q1ST_AUTH']) ? $_SERVER['HTTP_X_Q1ST_AUTH'] : '';
    if (!empty($_SERVER['PHP_AUTH_USER']) || stripos($h, 'Basic ') !== 0) { return; }
    $pair = base64_decode(substr($h, 6), true);
    if ($pair === false || strpos($pair, ':') === false) { return; }
    list($_SERVER['PHP_AUTH_USER'], $_SERVER['PHP_AUTH_PW']) = explode(':', $pair, 2);
})();

/* SEO fields: Yoast keeps them in post meta with no REST access; expose exactly these three to users who may edit the post. */
function q1st_seo_keys() {
    return array('_yoast_wpseo_title', '_yoast_wpseo_metadesc', '_yoast_wpseo_focuskw');
}

add_action('init', function () {
    if (!defined('WPSEO_VERSION')) { return; }
    foreach (get_post_types(array('show_in_rest' => true), 'names') as $type) {
        foreach (q1st_seo_keys() as $key) {
            register_post_meta($type, $key, array(
                'type' => 'string', 'single' => true, 'show_in_rest' => true,
                'sanitize_callback' => 'sanitize_text_field',
                'auth_callback' => function ($allowed, $meta_key, $post_id) { return current_user_can('edit_post', $post_id); },
            ));
        }
    }
});

/* What this site has, for the engine's connection test. Signed-in editors only; no content is returned. */
add_action('rest_api_init', function () {
    register_rest_route('q1st/v1', '/status', array(
        'methods' => 'GET',
        'permission_callback' => function () { return current_user_can('edit_posts'); },
        'callback' => 'q1st_status',
    ));
});

function q1st_status() {
    global $wpdb;
    $out = array(
        'plugin' => Q1ST_CONNECTOR_VERSION,
        'wordpress' => get_bloginfo('version'),
        'user' => wp_get_current_user()->user_login,
        'yoast' => defined('WPSEO_VERSION') ? WPSEO_VERSION : null,
        'seoKeys' => defined('WPSEO_VERSION') ? q1st_seo_keys() : array(),
        'translatepress' => null,
    );
    if (defined('TRP_PLUGIN_VERSION')) {
        $s = get_option('trp_settings', array());
        $tables = array();
        foreach ((array) $wpdb->get_col($wpdb->prepare('SHOW TABLES LIKE %s', $wpdb->esc_like($wpdb->prefix . 'trp_') . '%')) as $t) {
            $tables[$t] = $wpdb->get_col("SHOW COLUMNS FROM `" . esc_sql($t) . "`");
        }
        $out['translatepress'] = array(
            'version' => TRP_PLUGIN_VERSION,
            'defaultLanguage' => isset($s['default-language']) ? $s['default-language'] : null,
            'languages' => isset($s['translation-languages']) ? $s['translation-languages'] : array(),
            'urlSlugs' => isset($s['url-slugs']) ? $s['url-slugs'] : array(),
            'tables' => $tables,
        );
    }
    return $out;
}

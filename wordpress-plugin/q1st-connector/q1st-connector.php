<?php
/**
 * Plugin Name: Q1st Connector
 * Description: Lets the Quantum1st marketing engine publish approved articles to this site: signs it in on hosts that drop the login header, writes the SEO title and description, and reports what the site has.
 * Version: 0.2.0
 * Requires at least: 5.6
 * Requires PHP: 7.4
 * Author: Quantum1st
 * License: GPLv2 or later
 */

if (!defined('ABSPATH')) { exit; }

define('Q1ST_CONNECTOR_VERSION', '0.2.0');

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
    register_rest_route('q1st/v1', '/rendered/(?P<id>\\d+)', array(
        'methods' => 'GET',
        'permission_callback' => function ($r) { return current_user_can('edit_post', (int) $r['id']); },
        'callback' => 'q1st_rendered',
    ));
});

/* The text pieces a visitor's page shows for one post (title + content after WordPress's own filters), in order. */
function q1st_pieces($html) {
    $out = array();
    if (trim((string) $html) === '') { return $out; }
    $doc = new DOMDocument();
    libxml_use_internal_errors(true);
    $doc->loadHTML('<?xml encoding="utf-8"?><div>' . $html . '</div>', LIBXML_HTML_NOIMPLIED | LIBXML_HTML_NODEFDTD);
    libxml_clear_errors();
    $walk = function ($n) use (&$walk, &$out) {
        foreach ($n->childNodes as $c) {
            if ($c->nodeType === XML_TEXT_NODE) {
                $s = trim($c->nodeValue);
                if ($s !== '') { $out[] = $s; }
            } elseif ($c->nodeType === XML_ELEMENT_NODE && !in_array(strtolower($c->nodeName), array('script', 'style'), true)) {
                $walk($c);
            }
        }
    };
    $walk($doc);
    return $out;
}

function q1st_rendered($r) {
    $p = get_post((int) $r['id']);
    if (!$p) { return new WP_Error('not_found', 'No such post', array('status' => 404)); }
    $GLOBALS['post'] = $p;
    setup_postdata($p);
    $content = apply_filters('the_content', $p->post_content);
    wp_reset_postdata();
    return array('id' => $p->ID, 'title' => get_the_title($p), 'pieces' => q1st_pieces($content), 'html' => $content);
}

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
        $methods = array();
        foreach (array('TRP_Query' => array('insert_strings', 'update_strings', 'get_existing_translations', 'get_original_ids', 'insert_original_strings', 'get_table_name'),
                       'TRP_Translation_Render' => array('translate_page', 'process_strings', 'full_trim')) as $cls => $names) {
            foreach ($names as $m) {
                if (class_exists($cls) && method_exists($cls, $m)) {
                    $rm = new ReflectionMethod($cls, $m);
                    $methods[$cls . '::' . $m] = array_map(function ($a) { return ($a->isOptional() ? '?' : '') . '$' . $a->getName(); }, $rm->getParameters());
                }
            }
        }
        $dict = $wpdb->prefix . 'trp_dictionary_' . strtolower(isset($s['default-language']) ? $s['default-language'] : '') . '_ar';
        $sample = array();
        if (isset($tables[$dict])) {
            $sample = $wpdb->get_results("SELECT original, translated, status, block_type, original_id FROM `" . esc_sql($dict) . "` WHERE status = 2 AND (original LIKE '%&%' OR original LIKE '%\xe2\x80\x99%' OR original LIKE '%<%') ORDER BY id DESC LIMIT 6", ARRAY_A);
            $counts = $wpdb->get_results("SELECT status, block_type, COUNT(*) n FROM `" . esc_sql($dict) . "` GROUP BY status, block_type", ARRAY_A);
        }
        $out['translatepress'] = array(
            'methods' => $methods,
            'sample' => $sample,
            'counts' => isset($counts) ? $counts : array(),
            'version' => TRP_PLUGIN_VERSION,
            'defaultLanguage' => isset($s['default-language']) ? $s['default-language'] : null,
            'languages' => isset($s['translation-languages']) ? $s['translation-languages'] : array(),
            'urlSlugs' => isset($s['url-slugs']) ? $s['url-slugs'] : array(),
            'tables' => $tables,
        );
    }
    return $out;
}

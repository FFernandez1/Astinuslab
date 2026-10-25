// Fetch wrappers for the volatility workbench. Non-2xx responses reject with an
// Error carrying `status` and `detail` pulled from the JSON body when present.
import { csrfHeaders } from "../csrf.js";

export function getJSON(url) {
  return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) {
    if (!r.ok)
      return r
        .json()
        .catch(function () {
          return {};
        })
        .then(function (body) {
          var err = new Error(url + " → " + r.status);
          err.status = r.status;
          err.detail = body && body.detail;
          throw err;
        });
    return r.json();
  });
}

export function postJSON(url, payload) {
  return fetch(url, {
    method: "POST",
    headers: csrfHeaders("POST", {
      "Content-Type": "application/json",
      Accept: "application/json",
    }),
    body: JSON.stringify(payload),
  }).then(function (r) {
    return r
      .json()
      .catch(function () {
        return {};
      })
      .then(function (body) {
        if (!r.ok) {
          var err = new Error(url + " → " + r.status);
          err.status = r.status;
          err.detail = body && body.detail;
          throw err;
        }
        return body;
      });
  });
}

export function deleteJSON(url) {
  return fetch(url, {
    method: "DELETE",
    headers: csrfHeaders("DELETE", { Accept: "application/json" }),
  }).then(function (r) {
    return r
      .json()
      .catch(function () {
        return {};
      })
      .then(function (body) {
        if (!r.ok) {
          var err = new Error(url + " → " + r.status);
          err.status = r.status;
          err.detail = body && body.detail;
          throw err;
        }
        return body;
      });
  });
}

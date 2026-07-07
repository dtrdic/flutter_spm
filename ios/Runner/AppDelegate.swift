import UIKit
import Flutter
import Alamofire

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {

    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)

    guard
      let registrar = engineBridge.pluginRegistry.registrar(forPlugin: "spm_test"),
      let controller = registrar.messenger as? FlutterBinaryMessenger
    else {
      return
    }

    let channel = FlutterMethodChannel(
      name: "spm_test",
      binaryMessenger: controller
    )

    channel.setMethodCallHandler { call, result in

      if call.method == "testRequest" {

        AF.request("https://httpbin.org/get")
          .response { response in
            let code = response.response?.statusCode ?? -1
            result("SPM works: \(code)")
          }

      } else {
        result(FlutterMethodNotImplemented)
      }
    }
  }
}